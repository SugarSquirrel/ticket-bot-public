"""ddddocr 驗證碼辨識模組 — 支援自訓練 ONNX 模型、影像前處理與信心分數判斷"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from datetime import datetime

import cv2
import ddddocr
import numpy as np

from ticket_bot.config import CaptchaConfig
from ticket_bot.rl.bandit import ThresholdBandit

logger = logging.getLogger(__name__)


class CaptchaSolver:
    """tixcraft 文字驗證碼辨識器 — 支援預設模型與自訓練 ONNX 模型"""

    def __init__(self, config: CaptchaConfig):
        self.config = config
        self._using_custom = False

        # 優先：用 ddddocr import_onnx_path 掛載自訓練模型（與 ticket_hunter 同方式）
        if (
            config.custom_model_path
            and config.custom_charset_path
            and Path(config.custom_model_path).exists()
            and Path(config.custom_charset_path).exists()
        ):
            try:
                self.ocr = ddddocr.DdddOcr(
                    det=False,
                    ocr=False,
                    show_ad=False,
                    import_onnx_path=str(config.custom_model_path),
                    charsets_path=str(config.custom_charset_path),
                )
                self._using_custom = True
                logger.info(
                    "已掛載自訓練模型 (ddddocr import_onnx_path): %s",
                    config.custom_model_path,
                )
            except Exception as e:
                logger.error("掛載自訓練模型失敗，退回 ddddocr 預設: %s", e)
                self.ocr = ddddocr.DdddOcr(beta=config.beta_model, show_ad=False)
                if config.char_ranges:
                    self.ocr.set_ranges(config.char_ranges)
        else:
            self.ocr = ddddocr.DdddOcr(beta=config.beta_model, show_ad=False)
            if config.char_ranges:
                self.ocr.set_ranges(config.char_ranges)
            logger.info(
                "ddddocr 預設模型初始化完成 (beta=%s, ranges=%s)",
                config.beta_model,
                config.char_ranges,
            )

        self._collect_dir: Path | None = None
        if config.collect_dir:
            self._collect_dir = Path(config.collect_dir)
            self._collect_dir.mkdir(parents=True, exist_ok=True)
            logger.info("驗證碼收集已啟用，儲存到: %s", self._collect_dir)

        # RL: Thompson Sampling bandit 動態調整 confidence threshold
        self.bandit = ThresholdBandit()

    def _save_sample(self, image_bytes: bytes, text: str = "", confidence: float = 0.0):
        """儲存驗證碼圖片供訓練用"""
        if not self._collect_dir:
            return
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            label = text if text else "unknown"
            filename = f"{ts}_conf{confidence:.2f}_{label}.png"
            filepath = self._collect_dir / filename
            filepath.write_bytes(image_bytes)
            self._last_saved_path = filepath
            logger.debug("已儲存驗證碼: %s", filepath)
        except Exception as e:
            logger.warning("儲存驗證碼失敗: %s", e)

    def label_last_sample(self, correct_answer: str):
        """用正確答案重新標註最近收集的驗證碼"""
        path = getattr(self, "_last_saved_path", None)
        if not path or not path.exists():
            return
        try:
            new_name = path.name.rsplit("_", 1)[0] + f"_{correct_answer}.png"
            new_path = path.parent / new_name
            path.rename(new_path)
            labels_file = path.parent / "labels.json"
            labels = {}
            if labels_file.exists():
                labels = json.loads(labels_file.read_text())
            labels[new_path.name] = correct_answer
            labels_file.write_text(json.dumps(labels, ensure_ascii=False, indent=2))
            logger.info("已自動標註驗證碼: %s → %s", new_path.name, correct_answer)
        except Exception as e:
            logger.warning("自動標註失敗: %s", e)

    def preprocess(self, image_bytes: bytes) -> bytes:
        """針對 tixcraft 扭曲文字驗證碼的高級影像前處理"""
        nparr = np.frombuffer(image_bytes, np.uint8)
        img_cv = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
        if img_cv is None:
            return image_bytes

        # 1. 自適應二值化
        binary = cv2.adaptiveThreshold(
            img_cv, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 11, 2
        )
        
        # 2. 中值濾波去雜訊
        denoised = cv2.medianBlur(binary, 3)
        
        # 3. 形態學優化
        kernel = np.ones((2, 2), np.uint8)
        processed = cv2.morphologyEx(denoised, cv2.MORPH_OPEN, kernel)
        dilated = cv2.dilate(processed, kernel, iterations=1)
        
        # 4. 反轉回白底黑字
        final = cv2.bitwise_not(dilated)
        
        _, buffer = cv2.imencode(".png", final)
        return buffer.tobytes()

    def _run_ddddocr(self, processed_bytes: bytes) -> tuple[str, float]:
        """用 ddddocr 推論，相容 1.5 / 1.6+ 兩種 API 格式。"""
        result = self.ocr.classification(processed_bytes, probability=True)

        # 舊 API（1.5 以前）：可能直接回 str
        if isinstance(result, str):
            return result, 0.9  # 沒有信心度資訊，給高信心

        if not isinstance(result, dict):
            return "", 0.0

        # 1.5 風格：{"text": "abcd", "confidence": 0.9}
        if "text" in result:
            return result["text"], float(result.get("confidence", 0.9))

        # 1.6+ 風格：{"charsets": [...], "probability": [[...], ...]}
        charsets = result.get("charsets") or []
        probability = result.get("probability") or []
        if not charsets or not probability:
            return "", 0.0

        text_chars = []
        confs = []
        for pos_probs in probability:
            try:
                seq = list(pos_probs)
                best_idx = seq.index(max(seq))
                if 0 <= best_idx < len(charsets):
                    text_chars.append(charsets[best_idx])
                    confs.append(float(seq[best_idx]))
            except (ValueError, IndexError, TypeError):
                continue

        text = "".join(text_chars)
        confidence = (sum(confs) / len(confs)) if confs else 0.0
        return text, confidence

    def solve(self, image_bytes: bytes) -> tuple[str, float]:
        """辨識驗證碼 — 自訓練模型優先，沒設就用 ddddocr 預設"""
        try:
            # tixcraft_tm 模型用原始圖片就準，preprocess 反而會降低準度
            if self.config.preprocess and not self._using_custom:
                infer_bytes = self.preprocess(image_bytes)
            else:
                infer_bytes = image_bytes

            text, confidence = self._run_ddddocr(infer_bytes)
            text = "".join([c for c in text if c.isalpha()]).lower()

            if len(text) != 4:
                logger.warning("辨識結果長度無效 (長度: %d): %s", len(text), text)
                confidence = min(confidence, 0.2)

            model_tag = "tixcraft_tm" if self._using_custom else "ddddocr"
            logger.info("驗證碼結果 [%s]: %s (信心: %.2f)", model_tag, text, confidence)
            self._save_sample(image_bytes, text, confidence)
            return text, confidence
        except Exception as e:
            logger.error("辨識過程出錯: %s", e)
            return "", 0.0

    def solve_with_retry(self, fetch_image) -> str:
        """同步重試 — 使用 bandit 動態選擇 threshold"""
        threshold = self.bandit.select()
        text = ""
        for attempt in range(self.config.max_attempts):
            image_bytes = fetch_image()
            text, confidence = self.solve(image_bytes)
            if confidence >= threshold:
                logger.info("Bandit threshold=%.2f, 通過 (conf=%.2f)", threshold, confidence)
                return text
            logger.warning(
                "第 %d 次嘗試信心不足 (%.2f < %.2f)，刷新...",
                attempt + 1, confidence, threshold,
            )
        return text

    def report_captcha_result(self, success: bool):
        """回報 captcha 提交結果給 bandit 學習"""
        self.bandit.update(success=success)

    async def asolve_with_retry(self, fetch_image) -> str:
        """異步重試 — 使用 bandit 動態選擇 threshold"""
        threshold = self.bandit.select()
        text = ""
        for attempt in range(self.config.max_attempts):
            image_bytes = await fetch_image()
            text, confidence = self.solve(image_bytes)
            if confidence >= threshold:
                logger.info("Bandit threshold=%.2f, 通過 (conf=%.2f)", threshold, confidence)
                return text
            logger.warning(
                "第 %d 次嘗試信心不足 (%.2f < %.2f)，刷新...",
                attempt + 1, confidence, threshold,
            )
        return text
