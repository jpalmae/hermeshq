# 💡 Ejemplos de Uso - Gamma.app Skill

## Ejemplo 1: Propuesta Comercial Sixmanager

```bash
cd ~/.openclaw/workspace/skills/gamma-app

python3 scripts/create_presentation.py \
  --title "Propuesta Técnica/Comercial - Cliente ABC" \
  --content "
Sobre SIXMANAGER:
Somos una empresa de Servicios Tecnológicos con más de 15 años de experiencia.
Operaciones en Chile, Perú y Colombia.

SERVICIOS:
• Ciberseguridad
• Multicloud
• Infraestructura Digital
• Servicios Profesionales

CERTIFICACIONES:
ISO 9001 - Gestión de Calidad
ISO 27001 - Seguridad de la Información

PROPUESTA:
[Detalles específicos del cliente]
" \
  --language es-419 \
  --num-cards 12 \
  --export pdf \
  --instructions "Diseño corporativo profesional con colores azul oscuro y cyan"
```

## Ejemplo 2: Verificar y Descargar

```bash
# Verificar estado y obtener URLs
python3 scripts/get_download_urls.py \
  --generation-id "mJpTljWbckU397u90vP5t" \
  --poll \
  --max-attempts 30
```

## Ejemplo 3: Crear desde Template

```bash
python3 scripts/create_from_template.py \
  --template-id "propuesta-sixmanager-base" \
  --prompt "Adaptar para sector financiero. Incluir casos de banca y cumplimiento normativo." \
  --export pdf
```

## Ejemplo 4: Uso en Python

```python
#!/usr/bin/env python3
import sys
sys.path.insert(0, '/home/preventa/.openclaw/workspace/skills/gamma-app/scripts')

from gamma_client import GammaAPI

# Inicializar
gamma = GammaAPI()

# Crear presentación
result = gamma.create_presentation(
    input_text="""
    Pitch Deck: Solución de Ciberseguridad
    
    PROBLEMA:
    Las empresas enfrentan amenazas cibernéticas cada vez más sofisticadas.
    
    SOLUCIÓN:
    Sixmanager ofrece servicios de seguridad gestionada 24/7.
    
    BENEFICIOS:
    • Detección temprana de amenazas
    • Respuesta inmediata a incidentes
    • Cumplimiento normativo
    """,
    text_mode="generate",
    num_cards=8,
    text_options={"language": "es-419"},
    image_options={
        "source": "aiGenerated",
        "model": "imagen-3-pro"
    },
    additional_instructions="Diseño moderno y profesional para pitch de inversión",
    export_as="pdf"
)

# Obtener ID
gen_id = result['data']['generationId']
print(f"Presentación creada: {gen_id}")

# Verificar estado
from gamma_client import GammaDownloadAPI
download = GammaDownloadAPI()
urls = download.poll_until_ready(gen_id)

print(f"PDF: {urls['data'].get('pdfUrl')}")
```

## Ejemplo 5: Documentación Técnica

```bash
python3 scripts/create_presentation.py \
  --title "Documentación Técnica - Arquitectura de Red" \
  --content "
ARQUITECTURA DE RED SEGURA

1. CAPA PERIMETRAL
   - Firewall de próxima generación
   - IDS/IPS
   - VPN segura

2. CAPA DE RED
   - Segmentación VLAN
   - Switches gestionados
   - Monitoreo 24/7

3. CAPA DE APLICACIÓN
   - WAF
   - Autenticación multifactor
   - Auditoría continua
" \
  --language es-419 \
  --num-cards 15 \
  --export pdf \
  --text-mode preserve
```

## Ejemplo 6: Contenido para Redes Sociales

```bash
python3 scripts/create_presentation.py \
  --title "Post LinkedIn - Caso de Éxito" \
  --content "
Caso de Éxito: Implementación de SOC para Retail

CLIENTE: Cadena de retail con 50+ tiendas

DESAFÍO:
Amenazas cibernéticas y cumplimiento PCI DSS

SOLUCIÓN:
Implementación de SOC 24/7 con Sixmanager

RESULTADOS:
✓ 99.9% uptime
✓ Detección de 500+ amenazas
✓ Cumplimiento normativo alcanzado
" \
  --language es-419 \
  --num-cards 6 \
  --export pdf \
  --image-model "flux-1-pro"
```

## 🎯 Tips

1. **Siempre usar `--poll`** para obtener URLs automáticamente
2. **Especificar `--language es-419`** para español latinoamericano
3. **Usar `--export pdf`** si necesitas archivo descargable
4. **Incluir instrucciones detalladas** para mejor resultado visual

---

Para más ejemplos, revisa el archivo SKILL.md principal.
