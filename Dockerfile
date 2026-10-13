FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY release_alert/release_alert.py /app/

CMD ["python", "release_alert.py"]
