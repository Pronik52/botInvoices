FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY invoice_maker ./invoice_maker
COPY defaults.example.json ./defaults.json
COPY templates/examples ./templates
USER 1001:1001
CMD ["python", "-m", "invoice_maker"]
