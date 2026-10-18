# matches .python-version
FROM python:3.14-slim

WORKDIR /app

# exact locked versions from uv.lock -> reproducible image on any machine
COPY pyproject.toml uv.lock .python-version ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev

COPY . .
