FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.title="VerdantFlare Blender application" \
      org.opencontainers.image.version="0.1.1" \
      org.opencontainers.image.source="https://github.com/verdantflarehub/verdantflare-app-blender"
WORKDIR /app
COPY app/ /app/app/
COPY runtime/mcp-bridge/bridge.py /app/runtime/mcp-bridge/bridge.py
RUN mkdir /state && chown 10001:10001 /state
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 BLENDER_DATABASE=/state/blender.db
EXPOSE 8080
CMD ["python", "app/server.py"]
