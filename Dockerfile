# syntax=docker/dockerfile:1.7
FROM python:3.11.13-slim-bookworm AS builder
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.11.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 narang && useradd --uid 10001 --gid 10001 --no-create-home narang
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-deps /wheels/*.whl && rm -rf /wheels
USER 10001:10001
WORKDIR /app
# Runtime platforms must set readOnlyRootFilesystem=true and mount a size-limited /tmp tmpfs.
CMD ["python", "-c", "import narang_rider; print('runtime image ready; listener requires an approved deployment adapter')"]
