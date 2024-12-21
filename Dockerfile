# Use the official Python image.
FROM python:3.10-slim

# Set the working directory.
WORKDIR /app

# Copy the local code to the container image.
COPY . .

# Install dependencies.
RUN pip install -r requirements.txt

# Run the application.
CMD ["python", "main.py"]
