FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:0

WORKDIR /workspace

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tk \
        xauth \
        libx11-6 \
        libxext6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-forecast.txt .
RUN pip install --no-cache-dir -r requirements-forecast.txt

COPY forecast_credit_risk.py report_sentiment.py test_forecast_credit_risk.py test_report_sentiment.py model_gui_advanced.py ./

ENTRYPOINT ["python"]
CMD ["forecast_credit_risk.py", "--help"]
