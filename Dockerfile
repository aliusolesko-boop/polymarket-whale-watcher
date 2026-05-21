FROM python:3.12-slim

WORKDIR /app

# Copy dependency definition and source code
COPY pyproject.toml .
COPY src/ src/

# Install dependencies
RUN pip install --no-cache-dir .

# Create data directories
RUN mkdir -p data reports daily_briefings

# Default command
CMD ["python", "-m", "src.main", "run"]
