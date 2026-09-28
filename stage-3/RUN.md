# Tablekeeper stage 3

From this directory:

```sh
docker build -t tablekeeper-stage-3 .
docker run --rm -e PORT=8080 -p 8080:8080 tablekeeper-stage-3
```

`GET http://localhost:8080/health` reports readiness. Open `http://localhost:8080/` for the browser product. The service keeps state in memory; reset and import replace it atomically. No network or setup is needed at runtime.
