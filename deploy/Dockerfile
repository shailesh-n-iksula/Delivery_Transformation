# UNTESTED: Docker was not available where this was written. A starting point for the tech team, not a finished image.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --create-home --uid 10001 dcp
WORKDIR /app
COPY dcp ./dcp
COPY config ./config
COPY prompts ./prompts
COPY templates ./templates
COPY runner ./runner
# projects/ and data/ are mounted, never baked into the image: they hold client information.
USER dcp
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"
# The server refuses to listen on 0.0.0.0 without DCP_API_TOKEN set.
CMD ["python3", "-m", "dcp", "serve", "--host", "0.0.0.0", "--port", "8080"]
