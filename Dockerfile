FROM python:3.12-slim

WORKDIR /app

RUN pip install --upgrade pip setuptools wheel

COPY . .

RUN pip install requests && \
    pip install -e .

CMD ["python", "-m", "src.main", "run"]
