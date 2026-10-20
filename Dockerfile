FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY engine engine
COPY channels channels
COPY settings.yaml .
CMD ["python", "-m", "engine", "run"]
