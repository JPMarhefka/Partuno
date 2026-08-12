FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/partuno \
    PORT=8000

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN addgroup --system partuno \
    && adduser --system --ingroup partuno --home /home/partuno partuno \
    && mkdir -p /home/partuno \
    && chown -R partuno:partuno /home/partuno

COPY --chown=partuno:partuno config.py credentials.py client.py models.py services.py identity.py distributor_models.py distributors.py normalization.py mouser_client.py mouser_services.py multi_distributor.py mouser_mcp.py rest_auth.py mcp_server.py main.py app.py partuno.py ./

USER partuno

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/health', timeout=3)"

CMD ["sh", "-c", "exec uvicorn app:app --host ${HOST:-0.0.0.0} --port ${PORT:-8000}"]
