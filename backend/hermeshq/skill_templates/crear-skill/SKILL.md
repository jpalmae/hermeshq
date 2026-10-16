---
name: crear-skill
description: "Ayuda al usuario a crear o mejorar una skill SIN que sepa de herramientas ni scripts: lo entrevistas en lenguaje simple, usas lo que ya existe y la dejas validada con hq_skill_crear. Úsala cuando digan 'quiero una skill para…', 'crea una skill', 'mejora esta skill' o 'esto siempre lo hago así, recuérdalo'. No la uses para ejecutar la tarea en sí."
---

# Crear o mejorar una skill

El usuario te dice QUÉ quiere lograr, con sus palabras. **Tú** averiguas cómo hacerlo con lo que el sistema ya tiene. Nunca le pidas que sepa de herramientas, scripts o permisos.

## Reglas de oro
- **El usuario tiene libertad para crear y afinar sus skills.** Se crean y modifican con `hq_skill_crear`: rellenas campos cortos y la herramienta compone, valida y guarda. Una skill combina herramientas que ya existen y puede llevar **scripts propios de Python** (`scripts`: hasta 3, se validan: sin internet, sin comandos del sistema, sin claves, solo rutas del espacio de trabajo). No metas código ni HTML dentro del texto de la skill.
- Si el usuario necesita algo que ninguna herramienta cubre, díselo con claridad y propón pedírselo al administrador. Si es una rutina (vigilar correos, resumir cada semana), combínala con `hq_schedule_request` tras crear la skill.
- Máximo 6 herramientas por mensaje. La entrevista, el plan y la creación van en mensajes distintos.
- Todas las preguntas juntas, en un solo mensaje, en lenguaje simple. No preguntes lo que puedes ver tú.
- No crees una skill nueva si ya existe una parecida (administrada o del usuario): propón usarla o mejorarla.
- Nada de datos inventados: rutas, ids o cifras que no hayas verificado. No uses nombres de skills administradas.
- Razona poco: los campos son cortos. No redactes la skill completa en tu razonamiento.

## Enseña mientras creas (que el usuario aprenda a hacerlo bien)
- Antes de la entrevista, si es su primera skill, ofrécele en 3 líneas la guía: `hq_skill_crear(accion="guia")`.
- Cada validación devuelve `consejos`: cuéntaselos en simple (qué mejora y por qué), no solo los apliques.
- Al crear, explícale en 2 líneas por qué quedó bien y propónle probarla con 3 casos (normal, con datos faltantes, de otra persona).
- **Para que otros la usen:** no lleva datos personales (correos, rutas con su nombre): se los pide al usuario cada vez. Con su sí, `hq_skill_crear(accion="compartir", nombre=…, aprobado_por_el_usuario=true)`: queda para todos los agentes (si lleva scripts, la revisa antes el administrador). `accion="biblioteca"` muestra lo que ya compartieron otros: revísalo ANTES de crear algo parecido.

## Flujo (3 mensajes)
1. **Entrevista.** Pregunta, todo junto: ¿qué quieres lograr, en una frase? ¿Con qué palabras lo pedirías (dos ejemplos)? ¿Qué debo entregarte (resumen, archivo, correo) y a quién? ¿Qué cambia cada vez y qué es fijo? ¿Algo que NUNCA debo hacer o que siempre debo confirmar?
2. **Revisar y validar.** Lee `catalogo-de-capacidades`, mira tus skills con `hq_skill_crear(accion="listar")`. Luego llama `hq_skill_crear(accion="validar", nombre, descripcion, frases, pasos:[{texto, herramienta}], reglas, entrega, ejemplo)`. Si devuelve errores, corrígelos y vuelve a validar (máx. 3 veces); si dice que la herramienta no existe, díselo al usuario y propón pedírselo al administrador. Muestra al usuario el plan en 5 líneas (nombre, qué hace, pasos con herramienta, qué confirmará, qué entrega) y los avisos si hubo. Espera el sí.
3. **Crear y probar.** Con su sí: `hq_skill_crear(accion="crear", aprobado_por_el_usuario=true, …los mismos campos)`. Dile qué hace, cómo pedirla (su frase de ejemplo) y pídele que la pruebe pegando ese ejemplo en un mensaje nuevo. Para ajustarla después: mismos campos con `reemplazar=true` (se guarda un respaldo).

## Mejorar una skill propia
1. `hq_skill_crear(accion="listar")` y lee la que corresponda con `skill_view`.
2. Cuéntale al usuario, en simple, qué problemas ves y qué cambiarías. Espera el sí.
3. Valida y crea con `reemplazar=true`. Para quitarla: `accion="eliminar"` (se puede recuperar).
Las skills administradas no se editan, pero el usuario SÍ puede afinarlas: reglas propias con `hq_skill_crear(accion="ajustar", base=…, reglas=[…])`.

## Verificación antes de entregar
- [ ] La validación dio 0 errores y revisaste los avisos.
- [ ] Cada paso con herramienta usa una que existe (la validación lo comprueba).
- [ ] Hay confirmaciones donde algo es irreversible (enviar, crear, borrar).
- [ ] El usuario dijo que sí al plan y sabe cómo pedirla.
