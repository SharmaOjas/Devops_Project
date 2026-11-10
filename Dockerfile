FROM python:3.10-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    DSW_WEB_HOST=0.0.0.0 \
    DSW_WEB_PORT=5000

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000
# Same image runs the web app (default), the collector, and the DB setup job; k8s overrides the command.
CMD ["python", "app.py"]
