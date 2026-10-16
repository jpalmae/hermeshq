---
name: nombre-corto-en-minusculas
description: "QUÉ hace, en una frase, y CUÁNDO usarla (ej. 'cuando pidan X'). Si hay otra skill parecida, indica CUÁNDO NO usarla y cuál usar en su lugar."
---

# Nombre de la skill

<!-- Una o dos líneas: para qué sirve y qué entrega al final (un archivo, un resumen, un correo...). -->

## Antes de empezar (siempre)
1. Revisa tu catálogo de capacidades (skill `catalogo-de-capacidades`) y tus herramientas: usa lo que ya existe y **no armes un método propio** si ya hay uno.
2. Haz **todas las preguntas juntas, en un solo mensaje**, solo por lo que falte (datos, nombres, fechas, destino). No preguntes lo que ya te dijeron ni lo que puedes ver tú.
3. Si algo no se puede hacer, **verifícalo antes de decir "no puedo"** y propón la alternativa más cercana.

## Cuándo usarla
<!-- Ejemplos de frases reales del usuario. Ej.: "arma el informe de la semana", "resume mis correos". -->
- ...

## Cuándo NO usarla
<!-- Casos parecidos que son de otra skill. Ej.: "para editar uno ya hecho, usa la skill X". -->
- ...

## Flujo (máximo 6 pasos; una herramienta por paso)
<!-- Cada paso: qué herramienta o script usar y con qué tope. Llamadas de a una, nunca en paralelo.
     Lo mecánico (armar un archivo, convertir, calcular) va en un script de scripts/ y se ejecuta en UNA llamada. -->
1. ...
2. ...
3. ...

## Reglas de oro
- **Tiempo:** máximo 6 herramientas por mensaje. Si falta trabajo, entrega lo que tengas, di qué falta y sigue en el próximo mensaje.
- **Topes:** <!-- cuántos correos listar, cuántos abrir, cuántos caracteres leer, tamaño máximo de archivos -->.
- **No inventes:** rutas, ids, nombres de sitio, cifras ni fuentes. Busca primero y usa lo que devuelve la herramienta. Cifras siempre con fuente.
- **Confirma antes de lo irreversible** (enviar, crear o borrar): muestra un resumen y espera el sí. No repitas una pregunta ya respondida.
- **Avisa con claridad** cuando algo salió por un camino alternativo (por ejemplo, un archivo enviado como enlace).

## Verificación (antes de entregar)
<!-- Lista corta y concreta. Mejor aún: que el script la haga solo y devuelva los problemas. -->
- [ ] ...
- [ ] ...
- [ ] Lo entregado coincide con lo que pidió el usuario (revísalo contra su pedido, no contra tu plan).

## Entrega
<!-- Qué se le entrega y cómo: archivo con MEDIA:, enlace de OneDrive, correo, mensaje. Un resumen de 3 líneas: qué hiciste, qué supusiste, qué falta. -->

## Recursos
<!-- Solo si existen. Indica CUÁNDO abrir cada uno (no cargar todo siempre). -->
- `scripts/...` — qué hace y cómo ejecutarlo.
- `references/...` — detalle que solo se abre cuando hace falta.

## Limitaciones conocidas
<!-- Cada una con FECHA y cómo se comprobó. Revísalas cuando cambien las herramientas. Si no hay, borra la sección. -->
- (AAAA-MM-DD, probado así: ...) ...
