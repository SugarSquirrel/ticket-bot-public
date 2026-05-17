"""ddddocr 驗證碼辨識模組 — 支援自訓練 ONNX 模型、影像前處理與信心分數判斷"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from datetime import datetime

import cv2
import ddddocr
import numpy as np
from PIL import Image

from ticket_bot.config import CaptchaConfig
from ticket_bot.rl.bandit import ThresholdBandit

logger = logging.getLogger(__name__)


class CaptchaSolver:
    """tixcraft 文字驗證碼辨識器 — 支援預設模型與自訓練 ONNX 模型"""

    @staticmethod
    def _build_charset_mapping(chars: list[str]) -> dict[int, str]:
        if chars and chars[0] == "":
            return {i: c for i, c in enumerate(chars)}
        mapping = {i + 1: c for i, c in enumerate(chars)}
        mapping[0] = ""
        return mapping

    @classmethod
    def _load_charset_mapping(cls, charset_path: Path) -> dict[int, str]:
        raw_text = charset_path.read_text(encoding="utf-8")

        try:
            charset_data = json.loads(raw_text)
        except json.JSONDecodeError:
            chars = [line.strip() for line in raw_text.splitlines() if line.strip()]
            return cls._build_charset_mapping(chars)

        if isinstance(charset_data, dict) and "charset" in charset_data:
            return cls._build_charset_mapping(list(charset_data["charset"]))
        if isinstance(charset_data, list):
            return cls._build_charset_mapping(charset_data)
        return {int(k): v for k, v in charset_data.items()}

    def __init__(self, config: CaptchaConfig):
        self.config = config
        self._ort_session = None
        self._idx_to_char = {}

        if config.custom_model_path and Path(config.custom_model_path).exists():
            # 載入自訓練 ONNX 模型
            import onnxruntime as ort

            try:
                self._ort_session = ort.InferenceSession(str(config.custom_model_path))
                logger.info("自訓練 ONNX 模型已載入: %s", config.custom_model_path)
                
                # 載入字元集
                charset_path = Path(config.custom_charset_path) if config.custom_charset_path else None
                if charset_path and charset_path.exists():
                    self._idx_to_char = self._load_charset_mapping(charset_path)
                
                self.ocr = ddddocr.DdddOcr(beta=config.beta_model, show_ad=False)
            except Exception as e:
                logger.error("載入自定義模型失敗，切換回預設模型: %s", e)
                self.ocr = ddddocr.DdddOcr(beta=config.beta_model)
        else:
            self.ocr = ddddocr.DdddOcr(beta=config.beta_model)
            if config.char_ranges:
                self.ocr.set_ranges(config.char_ranges)
            logger.info("ddddocr 初始化完成 (beta=%s, ranges=%s)", config.beta_model, config.char_ranges)

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

    def _run_custom_model(self, processed_bytes: bytes) -> tuple[str, float]:
        """用自訓練 ONNX 模型推論"""
        img_p = Image.open(io.BytesIO(processed_bytes)).convert("L").resize((160, 64))
        arr = np.array(img_p, dtype=np.float32) / 255.0
        tensor = arr[np.newaxis, np.newaxis, :, :]

        input_name = self._ort_session.get_inputs()[0].name
        output = self._ort_session.run(None, {input_name: tensor})[0]

        indices = output[:, 0, :].argmax(axis=1)
        chars = []
        prev = -1
        for idx in indices:
            if idx != 0 and idx != prev:
                char = self._idx_to_char.get(idx, "")
                if char:
                    chars.append(char)
            prev = idx
        text = "".join(chars)

        probs = np.exp(output) / np.sum(np.exp(output), axis=2, keepdims=True)
        confidence = float(np.mean(np.max(probs[:, 0, :], axis=1)))
        return text, confidence

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
        """辨識驗證碼圖片 — 雙模型交叉驗證提高準確率"""
        try:
            processed_bytes = self.preprocess(image_bytes)

            if self._ort_session:
                custom_text, custom_conf = self._run_custom_model(processed_bytes)
                custom_text = "".join([c for c in custom_text if c.isalpha()]).lower()

                # 同時跑 ddddocr 做交叉驗證
                ocr_text, ocr_conf = self._run_ddddocr(processed_bytes)
                ocr_text = "".join([c for c in ocr_text if c.isalpha()]).lower()

                logger.info("雙模型: custom=%s(%.2f) ddddocr=%s(%.2f)",
                            custom_text, custom_conf, ocr_text, ocr_conf)

                if len(custom_text) == 4 and len(ocr_text) == 4:
                    if custom_text == ocr_text:
                        # 兩個模型一致 → 高信心
                        text, confidence = custom_text, min(custom_conf, ocr_conf) + 0.05
                    else:
                        # 不一致 → 逐字投票，取信心高的那個字
                        text_chars = []
                        for i in range(4):
                            if custom_text[i] == ocr_text[i]:
                                text_chars.append(custom_text[i])
                            elif custom_conf > ocr_conf:
                                text_chars.append(custom_text[i])
                            else:
                                text_chars.append(ocr_text[i])
                        text = "".join(text_chars)
                        confidence = max(custom_conf, ocr_conf) * 0.9
                        logger.info("逐字投票結果: %s (conf=%.2f)", text, confidence)
                elif len(custom_text) == 4:
                    text, confidence = custom_text, custom_conf
                elif len(ocr_text) == 4:
                    text, confidence = ocr_text, ocr_conf
                else:
                    # 都不是 4 位，取信心高的
                    text = custom_text if custom_conf >= ocr_conf else ocr_text
                    confidence = 0.2
            else:
                text, confidence = self._run_ddddocr(processed_bytes)
                text = "".join([c for c in text if c.isalpha()]).lower()

            if len(text) != 4:
                logger.warning("辨識結果長度無效 (長度: %d): %s", len(text), text)
                confidence = min(confidence, 0.2)

            logger.info("驗證碼最終結果: %s (信心: %.2f)", text, confidence)
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
