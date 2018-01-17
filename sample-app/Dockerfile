FROM python:3.6-alpine3.7

# Install requirements
COPY requirements.txt /
RUN pip install -r /requirements.txt

# Copy source file
COPY hello.py /usr/src/app/

ENV AWS_DEFAULT_REGION us-west-2

EXPOSE 80

CMD ["python", "/usr/src/app/hello.py"]
