import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { I18nProvider } from "../../lib/i18n";

let rol = "user";
const consulta = (data: unknown) => ({ data, isLoading: false });
const mutacion = () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false });

vi.mock("../../stores/sessionStore", () => ({
  useSessionStore: (selector: (s: { user: { id: string; role: string } }) => unknown) =>
    selector({ user: { id: "u1", role: rol } }),
}));
vi.mock("../../api/agents", () => ({
  useAgent: () => consulta({ id: "ag-1", name: "agente", friendly_name: "Agente Uno", status: "running" }),
  useAgentAction: mutacion,
  useUpdateAgent: mutacion,
  useDeleteAgent: mutacion,
}));
vi.mock("../../api/providers", () => ({ useProviders: () => consulta([]) }));
vi.mock("../../api/secrets", () => ({ useSecrets: () => consulta([]) }));
vi.mock("../../api/hermesVersions", () => ({ useHermesVersions: () => consulta([]) }));
vi.mock("../../api/logs", () => ({ useLogs: () => consulta({ pages: [] }) }));
vi.mock("../../api/runtimeLedger", () => ({ useRuntimeLedger: () => consulta([]) }));
vi.mock("../../api/tasks", () => ({ useTasks: () => consulta([]), useCreateTask: mutacion }));
const vacio = () => null;
vi.mock("../../components/AgentMessagingPanel", () => ({ AgentMessagingPanel: vacio }));
vi.mock("../../components/AgentAvatar", () => ({ AgentAvatar: vacio }));
vi.mock("../../components/AgentM365ScopesPanel", () => ({ AgentM365ScopesPanel: vacio }));
vi.mock("../../components/AgentSkillsPanel", () => ({ AgentSkillsPanel: vacio }));
vi.mock("../../components/AgentTerminal", () => ({ AgentTerminal: vacio }));
vi.mock("../../components/WorkspacePanel", () => ({ WorkspacePanel: () => <div>panel-workspace</div> }));
vi.mock("../../components/MarkdownText", () => ({ MarkdownText: vacio }));
vi.mock("./V2AgentIntegrationsTab", () => ({ V2AgentIntegrationsTab: vacio }));
vi.mock("./V2AgentConfigTab", () => ({ V2AgentConfigTab: vacio }));

const { V2AgentDetailPage } = await import("./V2AgentDetailPage");

function pintar() {
  return render(
    <MemoryRouter initialEntries={["/agents/ag-1"]}>
      <I18nProvider locale="es">
        <Routes>
          <Route path="/agents/:agentId" element={<V2AgentDetailPage />} />
        </Routes>
      </I18nProvider>
    </MemoryRouter>,
  );
}

describe("V2AgentDetailPage: workspace del agente compartido", () => {
  it("un usuario no administrador no ve la pestaña del workspace (el backend responde 403)", () => {
    rol = "user";
    pintar();
    expect(screen.queryByText("Workspace")).toBeNull();
  });

  it("un administrador sí la ve", () => {
    rol = "admin";
    pintar();
    expect(screen.getByText("Workspace")).toBeTruthy();
  });
});
