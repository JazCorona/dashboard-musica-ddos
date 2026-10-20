# Imagen del generador de carga. Va FUERA de las 3 imágenes de los servicios:
# se ejecuta como un contenedor aparte (o directo con python en el host).
FROM python:3.12-alpine
WORKDIR /app
COPY flood.py sweep.py /app/
RUN chmod +x /app/flood.py /app/sweep.py
# Sin proceso por defecto: se invoca con `docker run ... python flood.py HOST PUERTO`.
ENTRYPOINT ["python"]
CMD ["flood.py", "--help"]
