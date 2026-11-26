import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "../lib/i18n";
import type { ScheduledTask } from "../types/api";

const aprobar = vi.fn<(datos: unknown) => Promise<object>>(async () => ({}));
const actualizar = vi.fn<(datos: unknown) => Promise<object>>(async () => ({}));
let cronos: ScheduledTask[] = [];

vi.mock("../api/agents", () => ({
  useAgents: () => ({ data: [{ id: "ag-1", name: "agente", friendly_name: "Agente Uno" }] }),
}));
vi.mock("../stores/sessionStore", () => ({
  useSessionStore: (selector: (s: { user: { id: string } }) => unknown) => selector({ user: { id: "duena" } }),
}));
vi.mock("../api/scheduledTasks", async (original) => {
  const real = await original<typeof import("../api/scheduledTasks")>();
  const mutacion = (fn: (...a: unknown[]) => unknown) => ({ mutateAsync: fn, isPending: false, variables: undefined });
  return {
    ...real,
    useScheduledTasks: () => ({ data: cronos }),
    useCreateScheduledTask: () => mutacion(vi.fn()),
    useDeleteScheduledTask: () => mutacion(vi.fn()),
    useRunScheduledTaskNow: () => mutacion(vi.fn()),
    useUpdateScheduledTask: () => mutacion(actualizar),
    useApproveScheduledTask: () => mutacion(aprobar),
  };
});

const { ScheduledTasksPage } = await import("./ScheduledTasksPage");

function cron(extra: Partial<ScheduledTask> = {}): ScheduledTask {
  return {
    id: "c-1", agent_id: "ag-1", name: "Resumen diario", cron_expression: "30 8 * * 1-5",
    prompt: "Resume mis correos no leídos.", enabled: false, timezone: "America/Santiago", owner_user_id: "duena",
    approval_status: "pending", created_by_agent_id: "ag-1", notify_public_chat: false,
    output_format: "## Resumen de correos\n| Remitente | Asunto |", definition_version: 2,
    last_run: null, next_run: null, created_at: "2026-11-10T00:00:00Z", updated_at: "2026-11-10T00:00:00Z", ...extra,
  };
}

function pintar() {
  return render(
    <MemoryRouter>
      <I18nProvider locale="es">
        <ScheduledTasksPage />
      </I18nProvider>
    </MemoryRouter>,
  );
}

describe("ScheduledTasksPage: formato de salida versionado", () => {
  beforeEach(() => {
    aprobar.mockClear();
    actualizar.mockClear();
  });

  it("muestra el formato y su versión", () => {
    cronos = [cron()];
    pintar();
    expect(screen.getByText(/versión 2/)).toBeTruthy();
    expect(screen.getByText(/## Resumen de correos/)).toBeTruthy();
  });

  it("sin formato lo dice", () => {
    cronos = [cron({ output_format: null, definition_version: 1 })];
    pintar();
    expect(screen.getByText("Sin formato fijo")).toBeTruthy();
  });

  it("la aprobación muestra el formato y lo envía", async () => {
    cronos = [cron()];
    const confirmar = vi.spyOn(window, "confirm").mockReturnValue(true);
    pintar();
    fireEvent.click(screen.getByText("Aprobar y activar"));
    await waitFor(() => expect(aprobar).toHaveBeenCalled());
    expect(confirmar.mock.calls[0][0]).toContain("Formato de salida (versión 2)");
    expect(confirmar.mock.calls[0][0]).toContain("| Remitente | Asunto |");
    expect(aprobar.mock.calls[0][0]).toMatchObject({ output_format: "## Resumen de correos\n| Remitente | Asunto |" });
    confirmar.mockRestore();
  });

  it("editar permite cambiar el formato y vaciarlo lo quita", async () => {
    cronos = [cron({ approval_status: "approved", enabled: true })];
    pintar();
    fireEvent.click(screen.getByText("Editar"));
    const campos = screen.getAllByRole("textbox");
    const formato = campos.find((c) => (c as HTMLTextAreaElement).value.startsWith("## Resumen")) as HTMLTextAreaElement;
    fireEvent.change(formato, { target: { value: "   " } });
    fireEvent.click(screen.getByText("Guardar"));
    await waitFor(() => expect(actualizar).toHaveBeenCalled());
    expect(actualizar.mock.calls[0][0]).toMatchObject({ id: "c-1", output_format: null });
  });
});
