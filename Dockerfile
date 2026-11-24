FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENCE-AGPL-3.0.txt ./
COPY concept_digger/ ./concept_digger/
RUN pip install --no-cache-dir -e ".[server]"

ENV PORT=5007
EXPOSE 5007

CMD ["uvicorn", "concept_digger.main:app", "--host", "0.0.0.0", "--port", "5007","--reload"]
