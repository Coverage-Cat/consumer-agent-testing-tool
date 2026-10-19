# Setup-only build; this digest was exercised on linux/arm64.
FROM python:3.14.4-slim@sha256:2ca02f32b4d9d893863367ce07ec1972819f476dd38d8612f2a9cb6a41cbb727
RUN apt-get update && apt-get install -y --no-install-recommends iptables=1.8.11-2 util-linux=2.41.5-0+deb13u1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home agent
COPY src/consumer_agent_testing_tool/__init__.py src/consumer_agent_testing_tool/fixture.py src/consumer_agent_testing_tool/fixture_service.py src/consumer_agent_testing_tool/fixture_cli.py /opt/lab/consumer_agent_testing_tool/
COPY containers/entrypoint.sh /usr/local/bin/lab-entrypoint
COPY containers/booking /usr/local/bin/booking
ENV PYTHONPATH=/opt/lab PYTHONDONTWRITEBYTECODE=1
RUN chmod 755 /usr/local/bin/lab-entrypoint /usr/local/bin/booking
ENTRYPOINT ["/usr/local/bin/lab-entrypoint"]
