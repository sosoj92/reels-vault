import unittest

from vault_agent_registry import AgentProfile
import vault_orchestrator as orchestrator


def agent(name):
    return AgentProfile(
        slug=name.lower().replace(" ", "-"),
        name=name,
        description=f"{name} description",
        path=f"engineering/{name.lower().replace(' ', '-')}.md",
        markdown=f"# {name}\nPlaybook",
    )


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.agents = [
            agent("Knowledge Graph Engineer"),
            agent("Data Engineer"),
            agent("Multi-Agent Systems Architect"),
            agent("AI Engineer"),
            agent("Autonomous Optimization Architect"),
            agent("DevOps Automator"),
            agent("SRE (Site Reliability Engineer)"),
            agent("Privacy Engineer"),
            agent("Security Architect"),
            agent("Minimal Change Engineer"),
            agent("Code Reviewer"),
            agent("Backend Architect"),
            agent("Codebase Onboarding Engineer"),
            agent("Software Architect"),
        ]

    def test_routes_knowledge_graph_task(self):
        result = orchestrator.select_agents(
            "analyser sources entities relations contradictions",
            agents=self.agents,
        )
        names = [item.name for item in result["agents"]]
        self.assertEqual(names[:2], ["Knowledge Graph Engineer", "Data Engineer"])
        self.assertEqual(result["routes"][0]["route"], "knowledge_graph")

    def test_limits_selected_agents(self):
        result = orchestrator.select_agents(
            "agent orchestration worker automation source relation security",
            agents=self.agents,
            max_agents=3,
        )
        self.assertLessEqual(len(result["agents"]), 3)

    def test_prompt_keeps_vault_guardrails(self):
        prompt = orchestrator.render_prompt_context(
            "review source claims",
            agents=self.agents,
            max_agents=2,
        )
        self.assertIn("Vault/SQLite is the source of truth", prompt)
        self.assertIn("SELECTED REASONING PLAYBOOKS", prompt)


if __name__ == "__main__":
    unittest.main()
