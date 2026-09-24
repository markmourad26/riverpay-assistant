FROM python:3.12-slim

WORKDIR /app
ENV HF_HOME=/app/.cache/huggingface \
    PIP_NO_CACHE_DIR=1

# CPU-only torch first - avoids pulling multi-GB CUDA wheels via sentence-transformers.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding model into the image so retrieval runs offline at runtime.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"
ENV HF_HUB_OFFLINE=1

COPY app/ ./app/
COPY eval/ ./eval/
COPY data/ ./data/
COPY cli.py run_eval.py check_citations.py streamlit_app.py ./

# Secrets are injected at run time (env_file / --env-file), never baked in.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/output /app/.cache/llm \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8501
CMD ["streamlit", "run", "streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
