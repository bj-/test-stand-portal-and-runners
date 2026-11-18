FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY app.py tasks.py smoke.py version.json ./
CMD ["python", "app.py"]
