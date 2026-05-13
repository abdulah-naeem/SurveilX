FROM python:3.11-slim

# Install system dependencies required for OpenCV and media streaming
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Create a dedicated user to avoid running container as root (ZeroGPU requirement)
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"

WORKDIR /app

# Install Python dependencies first to leverage Docker layer caching
COPY --chown=user:user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy remaining source code and models
COPY --chown=user:user . .

# Hugging Face Spaces exclusively forwards external traffic to port 7860
EXPOSE 7860

# Launch application on port 7860
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
