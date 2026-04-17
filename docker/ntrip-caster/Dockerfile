FROM python:3.12-alpine

WORKDIR /app

# Copy caster script (pure Python, no dependencies needed)
COPY ntrip_caster.py ./

# Default NTRIP port
EXPOSE 2101

# Environment defaults
ENV CASTER_PORT=2101
ENV SERVER_PASSWORD=testpass
ENV LOG_LEVEL=info

CMD ["python", "ntrip_caster.py"]
