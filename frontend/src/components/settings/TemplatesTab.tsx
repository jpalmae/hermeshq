import { FormEvent, useState } from "react";

import { useAgents } from "../../api/agents";
import { useCreateTemplate, useTemplates } from "../../api/templates";
import { useSessionStore } from "../../stores/sessionStore";

export function TemplatesTab() {
  const currentUser = useSessionStore((state) => state.user);
  const isAdmin = currentUser?.role === "admin";
  const { data: agents } = useAgents();
  const createTemplate = useCreateTemplate();
  const { data: templates } = useTemplates(isAdmin);

  const [templateName, setTemplateName] = useState("");
  const [templateDescription, setTemplateDescription] = useState("");

  async function submitTemplate(event: FormEvent) {
    event.preventDefault();
    await createTemplate.mutateAsync({
      name: templateName,
      description: templateDescription,
      config: {
        node_id: agents?.[0]?.id,
        name: `${templateName} Agent`,
        slug: templateName.toLowerCase().replace(/\s+/g, "-"),
        run_mode: "hybrid",
      },
    });
    setTemplateName("");
    setTemplateDescription("");
  }

  return (
    <section className="grid gap-6 xl:grid-cols-2">
      <form className="panel-frame p-6" onSubmit={submitTemplate}>
        <p className="panel-label">Templates</p>
        <h2 className="mt-2 text-2xl text-[var(--text-display)]">Agent presets</h2>
        <div className="mt-6 space-y-4">
          <label className="panel-field">
            <span className="panel-label">Name</span>
            <input value={templateName} onChange={(event) => setTemplateName(event.target.value)} />
          </label>
          <label className="panel-field">
            <span className="panel-label">Description</span>
            <textarea rows={4} value={templateDescription} onChange={(event) => setTemplateDescription(event.target.value)} />
          </label>
          <button className="panel-button-primary w-full" type="submit">
            Save template
          </button>
        </div>
      </form>

      <div className="panel-frame p-6">
        <p className="panel-label">Stored templates</p>
        <div className="mt-4 space-y-3">
          {(templates ?? []).map((template) => (
            <div key={String(template.id)} className="border-b border-[var(--border)] pb-3">
              <p className="mt-2 text-sm text-[var(--text-display)]">{String(template.name)}</p>
              <p className="mt-1 text-sm text-[var(--text-secondary)]">
                {String(template.description ?? "")}
              </p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
