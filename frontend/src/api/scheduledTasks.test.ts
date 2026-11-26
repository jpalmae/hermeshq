import { describe, expect, it } from "vitest";

import { cuerpoDeAprobacion, formatoParaGuardar } from "./scheduledTasks";

describe("formato de salida del cron", () => {
  it("vacío o con espacios se guarda como null (no crea una versión nueva)", () => {
    expect(formatoParaGuardar("")).toBeNull();
    expect(formatoParaGuardar("   \n")).toBeNull();
    expect(formatoParaGuardar(undefined)).toBeNull();
    expect(formatoParaGuardar("  ## Título  ")).toBe("## Título");
  });

  it("la aprobación envía exactamente prompt, horario y formato vistos", () => {
    expect(
      cuerpoDeAprobacion({ cron_expression: "30 8 * * 1-5", prompt: "Resume", output_format: "## Resumen\n| A |" }),
    ).toEqual({ cron_expression: "30 8 * * 1-5", prompt: "Resume", output_format: "## Resumen\n| A |" });
    expect(cuerpoDeAprobacion({ cron_expression: "0 9 * * *", prompt: "X", output_format: null }).output_format).toBeNull();
  });
});
