FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace

COPY requirements-forecast.txt .
RUN pip install --no-cache-dir -r requirements-forecast.txt

COPY forecast_credit_risk.py report_sentiment.py test_forecast_credit_risk.py test_report_sentiment.py ./

ENTRYPOINT ["python"]
CMD ["forecast_credit_risk.py", "--help"]
