FROM python:3.11-slim

# Install system dependencies required for OpenCV and media streaming
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Create a dedicated user and grant absolute workspace ownership to prevent Errno 13 Permission Denied crashes
RUN useradd -m -u 1000 user && mkdir -p /app && chown -R user:user /app

WORKDIR /app
USER user
ENV PATH="/home/user/.local/bin:$PATH"

# Install Python dependencies first to leverage Docker layer caching
COPY --chown=user:user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy remaining source code and models
COPY --chown=user:user . .

# Expose standard default port mapping
EXPOSE 7860

# Launch application dynamically binding to cloud-allocated network ports ($PORT) or defaulting to 7860
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-7860}
