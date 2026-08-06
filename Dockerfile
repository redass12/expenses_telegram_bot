FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgomp1 \
        libgl1 \
        libglib2.0-0 \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-fra \
        tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Bake the memory-efficient detector into the image so production never has to
# download a model while a Telegram request is waiting.
RUN python -c "from rapidocr import RapidOCR, EngineType, LangDet, LangRec, ModelType, OCRVersion; RapidOCR(params={'Global.log_level': 'warning', 'EngineConfig.onnxruntime.intra_op_num_threads': 1, 'EngineConfig.onnxruntime.inter_op_num_threads': 1, 'Det.engine_type': EngineType.ONNXRUNTIME, 'Det.lang_type': LangDet.CH, 'Det.model_type': ModelType.TINY, 'Det.ocr_version': OCRVersion.PPOCRV6, 'Rec.engine_type': EngineType.ONNXRUNTIME, 'Rec.lang_type': LangRec.CH, 'Rec.model_type': ModelType.SMALL, 'Rec.ocr_version': OCRVersion.PPOCRV6})"

COPY bot.py ./

CMD ["python", "bot.py"]
