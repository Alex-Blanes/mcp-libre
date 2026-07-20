# mcp-libre — LibreOffice MCP Server

MCP server para operaciones con documentos LibreOffice (Writer, Calc, Impress) desplegado en Docker en el NAS via Tailscale.

## Conexion

El servidor MCP se define en `~/.opencode/opencode.json`:

```json
{
  "mcp": {
    "libreoffice": {
      "type": "remote",
      "url": "http://100.88.134.15:8765/mcp"
    }
  }
}
```

## Antes de usar cualquier herramienta (LEER PRIMERO)

Este MCP corre en Docker en el NAS (Linux). No tiene acceso al sistema de archivos de tu maquina local (Windows).

### Regla de oro

Si el documento esta en tu maquina local (Windows) → **NO uses `path`**. Usa siempre `document_base64`.

### Que hacer si ves "Document not found"

1. **Diagnostico rapido**: llama a `get_server_info()` para verificar plataforma y version de LO
2. **Causa mas comun**: pasaste un path de Windows (`C:\Users\...`) al servidor remoto Linux
3. **Solucion**: lee el archivo local con tu MCP local (ej. Windows-MCP `FileSystem`), codificalo a base64, y pasalo como `document_base64=<contenido_b64>`

## Patron base64 (stateless) — Flujo completo

Todas las herramientas aceptan `document_base64` / `documents_base64` como alternativa a rutas de archivo. El servidor nunca accede al filesystem local del cliente — los documentos viajan como base64 en el request/response.

### Workflow tipico con OpenCode / Claude Code

```
Tienes un documento en C:\Users\...\documento.odt

PASO 1: Leer el archivo local
  → Usa tu MCP local (Windows-MCP FileSystem) para leer los bytes del archivo

PASO 2: Codificar a base64
  → Convierte los bytes leidos a base64

PASO 3: Llamar a la herramienta MCP con document_base64
  → Pasa el contenido codificado como document_base64=<b64>

PASO 4: Guardar el resultado (si la herramienta devuelve base64)
  → Decodifica el result_base64 y escribe al archivo local
```

### Ejemplo: leer texto de un documento

```python
import base64

# 1. Leer archivo local
with open("C:/Users/alex-/documento.odt", "rb") as f:
    doc_b64 = base64.b64encode(f.read()).decode("ascii")

# 2. Llamar al MCP
result = await client.call_tool("read_document_text", {
    "document_base64": doc_b64
})

# El resultado ya es texto plano
print(result.content)
```

### Ejemplo: modificar y guardar

```python
import base64

# 1. Leer archivo local
with open("C:/Users/alex-/documento.odt", "rb") as f:
    doc_b64 = base64.b64encode(f.read()).decode("ascii")

# 2. Insertar texto como cambio rastreado
result = await client.call_tool("insert_tracked_text", {
    "document_base64": doc_b64,
    "anchor_text": "Texto existente",
    "new_text": "Nuevo texto a insertar",
    "author": "Nombre Apellido",
    "insert_mode": "after"
})

# 3. Guardar el documento modificado
if result.structuredContent.get("success"):
    modified_b64 = result.structuredContent["document_base64"]
    with open("C:/Users/alex-/documento_modificado.odt", "wb") as f:
        f.write(base64.b64decode(modified_b64))
```

## Herramientas disponibles

| Tool | Input base64 | Output base64 | Descripcion |
|---|---|---|---|
| `get_server_info` | — | — | Info del servidor (OS, LO version, Docker, hints) |
| `create_document` | `return_base64=True` | `result_base64` | Crear documento nuevo |
| `read_document_text` | `document_base64` | — (texto plano) | Extraer texto |
| `convert_document` | `document_base64` | `result_base64` | Convertir formato |
| `get_document_info` | `document_base64` | — (metadatos) | Info del documento |
| `read_spreadsheet_data` | `document_base64` | — (datos CSV) | Leer hoja de calculo |
| `insert_text_at_position` | `document_base64` | `result_base64` | Insertar texto (start/end/replace) |
| `insert_tracked_text` | `document_base64` | `result_base64` | Insertar como control de cambios |
| `get_document_statistics` | `document_base64` | — (stats) | Estadisticas del documento |
| `search_documents` | `documents_base64` | — (resultados) | Buscar texto en documentos |
| `merge_text_documents` | `documents_base64` | `result_base64` | Fusionar documentos |
| `open_document_in_libreoffice` | `document_base64` | — | Abrir en LO GUI (usa tmp persistente) |
| `create_live_editing_session` | `document_base64` | — | Sesion de edicion en vivo |

## Despliegue (NAS)

```bash
# Build
ssh nas
cd ~/docker/mcp-libre
docker build -t mcp-libre .

# Run
docker run -d \
  --name mcp-libre \
  -p 8765:8765 \
  -e MCP_ALLOWED_HOSTS="100.88.134.15:8765" \
  --restart unless-stopped \
  mcp-libre

# Logs
docker logs mcp-libre

# Stop/remove
docker stop mcp-libre && docker rm mcp-libre
```

## Desarrollo local

```bash
# Activar venv y probar
cd src
python main.py --test

# O via MCP cliente de prueba
cd tests
python test_client.py
```
