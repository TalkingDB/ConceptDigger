FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV PORT=5007
EXPOSE 5007

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5007","--reload"]
