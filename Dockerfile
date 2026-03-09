FROM python:3.12.8-bookworm
LABEL authors="Eko Indarto"

ARG BUILD_DATE
ENV BUILD_DATE=$BUILD_DATE


# Combine apt-get commands to reduce layers
RUN apt-get update -y && \
    apt-get upgrade -y && \
    apt-get dist-upgrade -y && \
    apt-get install -y --no-install-recommends git curl postgresql-client && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN useradd -ms /bin/bash akmi

ENV PYTHONPATH=/home/akmi/fms/src
ENV BASE_DIR=/home/akmi/fms

WORKDIR ${BASE_DIR}


# Install uv.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy the application into the container.


# Create and activate virtual environment
# RUN python -m venv .venv
# RUN uv venv .venv
ENV APP_NAME="Filemetrix Service"
# ENV PATH="/home/akmi/fms/.venv/bin:$PATH"
# Copy the application into the container.
COPY src ./src
# Copy the env validator script into the image
COPY src/filemetrix/validate_env.py ./src/filemetrix/validate_env.py
#Temporary, will be removed later
#COPY conf ./conf
COPY pyproject.toml .
COPY README.md .
COPY uv.lock .


# Install dependencies

RUN uv sync --frozen --no-cache && chown -R akmi:akmi ${BASE_DIR}
USER akmi
RUN mkdir logs
# Run the application. Validate env before starting the app.
CMD ["/bin/sh", "-c", "/home/akmi/fms/.venv/bin/python /home/akmi/fms/src/filemetrix/validate_env.py && /home/akmi/fms/.venv/bin/python -m src.filemetrix.main"]

#CMD ["tail", "-f", "/dev/null"]
