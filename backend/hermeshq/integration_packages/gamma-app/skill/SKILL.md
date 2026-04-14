---
name: gamma-app
description: Crear presentaciones, documentos, webpages y contenido social profesional usando la integración gestionada de Gamma.app en HermesHQ.
---

# Gamma.app Skill

Usa esta skill cuando necesites generar contenido visual profesional con Gamma.app desde HermesHQ.

## Herramientas disponibles

La integración expone estas tools:

- `gamma_create_presentation`
- `gamma_create_document`
- `gamma_create_webpage`
- `gamma_create_social_post`
- `gamma_create_from_template`
- `gamma_get_generation_status`
- `gamma_wait_for_generation`

## Flujo recomendado

1. Si el contenido es para Sixmanager, leer primero:
   - `references/sixmanager_context.md`
   - `references/branding_sixmanager.md`
   - `references/language_codes.md`
2. Preparar el contenido base y el objetivo de la pieza.
3. Elegir la tool correcta.
4. Si quieres branding corporativo Sixmanager, usar:
   - `sixmanager_branding = true`
   - `logo_type = horizontal | vertical | iso`
5. Si necesitas el artefacto final exportado, pedir `export_as = pdf` o `pptx`.
6. Luego consultar el estado con:
   - `gamma_get_generation_status`
   - o `gamma_wait_for_generation`

## Reglas prácticas

- Para propuestas, decks y presentaciones comerciales, preferir `gamma_create_presentation`.
- Para políticas, documentos internos o propuestas narrativas, preferir `gamma_create_document`.
- Para landing pages o micrositios, usar `gamma_create_webpage`.
- Para piezas cortas de marketing, usar `gamma_create_social_post`.
- Para reutilizar una base visual existente, usar `gamma_create_from_template`.

## Notas de Sixmanager

Este paquete conserva la base de contexto y branding Sixmanager del skill original. Si el trabajo no es para Sixmanager, ignora esas referencias y usa instrucciones neutrales.

## Ejemplos

Revisa:
- `examples/ejemplos_uso.md`
