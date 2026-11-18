FROM python:3.12-slim
WORKDIR /example
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY app.py runner.py ./
EXPOSE 8080
CMD ["python", "app.py"]
