FROM registry.cn-qingdao.aliyuncs.com/wod/beagle-wind-vnc@sha256:ee0a990677694e9919cbedaae227f246867b5e183d1caa48b2fdc09d37e63305 AS desktop-client
FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.title="VerdantFlare Blender application" \
      org.opencontainers.image.version="0.1.5" \
      org.opencontainers.image.source="https://github.com/verdantflarehub/verdantflare-app-blender"
WORKDIR /app
COPY app/ /app/app/
COPY runtime/mcp-bridge/bridge.py /app/runtime/mcp-bridge/bridge.py
COPY --from=desktop-client /opt/bdwind/webrtc/ /app/webclient/
COPY app/webclient/embed.js /app/webclient/studio-embed.js
RUN python -c "from pathlib import Path; p=Path('/app/webclient/index.html'); p.write_text(p.read_text().replace('<head>', '<head><script src=\"studio-embed.js\"></script>'))"
RUN mkdir /state && chown 10001:10001 /state
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 BLENDER_DATABASE=/state/blender.db
EXPOSE 8080
CMD ["python", "app/server.py"]
