---
name: crear-skill
description: "Ayuda al usuario a crear o mejorar una skill SIN que sepa de herramientas ni scripts: lo entrevistas en lenguaje simple, usas lo que ya existe y la dejas probada. Úsala cuando digan 'quiero una skill para…', 'crea una skill', 'mejora esta skill' o 'esto siempre lo hago así, recuérdalo'. No la uses para ejecutar la tarea en sí."
---

# Crear o mejorar una skill

El usuario te dice QUÉ quiere lograr, con sus palabras. **Tú** averiguas cómo hacerlo con lo que el sistema ya tiene. Nunca le pidas que sepa de herramientas, scripts o permisos.

## Reglas de oro
- Máximo 6 herramientas por mensaje. La entrevista y el plan van en mensajes distintos de la creación.
- Todas las preguntas juntas, en un solo mensaje, en lenguaje simple. No preguntes lo que puedes ver tú.
- No crees una skill nueva si ya existe una parecida: propón mejorarla.
- No modifiques una skill ya existente sin mostrar el cambio y recibir un sí. Antes de editarla guarda una copia en `.hermes/SKILL.<nombre>.md.bak-<fecha>`.
- Nada de datos inventados en la skill: rutas, ids o cifras que no hayas verificado.

## Flujo
1. **Entrevista (1 mensaje).** Pregunta, todo junto: ¿qué quieres lograr, en una frase? ¿Un ejemplo real de cómo lo pedirías? ¿Qué debo entregarte (archivo, resumen, correo, otra cosa) y a quién? ¿Qué cambia cada vez y qué es fijo? ¿Hay algo que NUNCA debo hacer o que siempre debo confirmar?
2. **Revisar lo que ya existe.** Lee `catalogo-de-capacidades` y mira tus skills (`skills_list`). Anota qué herramientas y skills sirven para esto.
3. **Proponer el plan (1 mensaje corto, 5 líneas).** Qué skill vas a crear o mejorar, con qué herramientas, qué topes (tiempo, cantidad, tamaño), qué confirmaciones pedirá. Espera el sí.
4. **Crear.** Usa `skill_manage` (acción `create`, categoría `productivity` si no hay otra mejor) con la plantilla de `references/plantilla.md`. La skill debe pesar menos de 8 KB, tener una descripción que diga cuándo usarla y cuándo no, y un flujo de máximo 6 pasos.
5. **Lo mecánico, en un script.** Si la skill repite pasos que siempre son iguales (armar un archivo, convertir, calcular), propón guardarlos en `scripts/` y ejecutarlos en una sola llamada, en vez de que el modelo vuelva a escribir el código cada vez.
6. **Probar con el ejemplo real.** Ejecuta el caso que te dio el usuario. Mide cuántas herramientas usaste y cuánto tardó. Si pasaste de 6 herramientas o de 3 minutos, simplifica el flujo o pasa pasos a un script y vuelve a probar.
7. **Entregar en simple.** Dile qué hace la skill, cómo pedirla (una frase de ejemplo), qué límites tiene y qué copia guardaste. Sin tecnicismos.

## Mejorar una skill que ya existe
1. Léela y compárala con `catalogo-de-capacidades`: busca afirmaciones falsas ("no se puede…" que hoy sí se puede), herramientas que no existen, pasos que se pueden hacer en una sola llamada, lecturas innecesarias y llamadas en paralelo.
2. Cuéntale al usuario, en simple, qué problemas encontraste y qué cambiarías. Espera el sí.
3. Guarda la copia, aplica los cambios con `skill_manage` (acción `patch`), prueba con un caso real y compara el tiempo.

## Verificación antes de entregar
- [ ] La descripción dice cuándo usarla y cuándo no.
- [ ] El flujo tiene como máximo 6 pasos y usa las herramientas del catálogo (no inventa nombres).
- [ ] Tiene topes y confirmaciones donde hay algo irreversible (enviar, crear, borrar).
- [ ] Probaste con el ejemplo real y anotaste tiempo y número de herramientas.
- [ ] Pesa menos de 8 KB y no repite lo que ya dice el catálogo.
- [ ] El usuario sabe cómo pedirla.

## Recursos
- `references/plantilla.md` — plantilla de skill lista para rellenar. Ábrela solo en el paso 4.
