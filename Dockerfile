# One image for both the API (default command) and the ETL job (`geofuse run`).
FROM python:3.11-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.11-slim
RUN useradd --create-home --uid 10001 geofuse
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
USER geofuse
WORKDIR /home/geofuse
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/docs', timeout=2)"
CMD ["uvicorn", "geofuse.api:app", "--host", "0.0.0.0", "--port", "8000"]
