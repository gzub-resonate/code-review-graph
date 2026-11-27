"""Queue-to-exchange bindings declared in code: the ``QueueBinding`` data class and
``BindingBuilder``. Fixtures imitate ``SalesforceAmqpConfig`` / ``ProjectAdminAmqpConfig`` of
resonate/services-api and ``InsightDescriptionAmqpConfig`` of resonate/services-api-attribute."""

from pathlib import Path

from code_review_graph.parser import CodeParser

ACCOUNT = """
package com.example.account;
public class AccountAmqpConfig {
  public static final String EXCHANGE_ACCOUNT = "api.e.topic.account";
  public static final String ROUTING_KEY_ACCOUNT_PROVISION = "account.provision";
}
"""

SALESFORCE = """
package com.example.salesforce;
import com.example.common.QueueBinding;
@Configuration
public class SalesforceAmqpConfig {
  private static final String EXCHANGE = "api.e.topic.salesforce";
  private static final String CREATE_KEY = "account.created";
  public static final String CREATE_QUEUE = "api.q.salesforce.account.create";
  private static List<QueueBinding> queueBindings =
      Arrays.asList(
          new QueueBinding(CREATE_QUEUE, EXCHANGE, CREATE_KEY),
          new QueueBinding("api.q.salesforce.account.delete", EXCHANGE, "account.deleted"));
  @Bean
  public Declarables declarations() { return RabbitConfig.amqpDeclarations(queueBindings); }
}
"""

PROJECT_ADMIN = """
package com.example.projectadmin;
import com.example.account.AccountAmqpConfig;
@Configuration
public class ProjectAdminAmqpConfig {
  public static final String QUEUE = "api.q.projectadmin.account.created";
  private static List<QueueBinding> queueBindings = Arrays.asList(
      new QueueBinding(QUEUE, AccountAmqpConfig.EXCHANGE_ACCOUNT,
          AccountAmqpConfig.ROUTING_KEY_ACCOUNT_PROVISION));
}
"""

INSIGHT = """
package com.example.insight;
@Configuration
public class InsightDescriptionAmqpConfig {
  public static final String ATTRIBUTE_EXCHANGE = "api.e.topic.attribute";
  public static final String DEFINE_QUEUE = "api.q.attribute.define.insight-description";
  public static final String DEFINE_KEY = "attribute.define";

  @Bean
  public Queue insightDescriptionDefineQueue() {
    return new Queue(DEFINE_QUEUE, true);
  }

  @Bean
  public Binding insightDescriptionDefineBinding() {
    return BindingBuilder.bind(insightDescriptionDefineQueue())
        .to(new TopicExchange(ATTRIBUTE_EXCHANGE))
        .with(DEFINE_KEY);
  }

  @Bean
  public Binding unresolvedBinding(Queue q, TopicExchange ex) {
    return BindingBuilder.bind(q).to(ex).with("x.y");
  }
}
"""


def _parse(tmp_path: Path, rel: str, body: str, others: dict[str, str] | None = None):
    files = {"account/AccountAmqpConfig.java": ACCOUNT, rel: body, **(others or {})}
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _, edges = CodeParser(tmp_path).parse_file(tmp_path / rel)
    return [e for e in edges if e.kind == "BINDS"]


def _brief(edges):
    return {
        (e.target, e.extra["queue"], e.extra["binding_key"]) for e in edges
    }


def test_queue_binding_data_class_in_a_static_field_is_a_declared_binding(tmp_path: Path) -> None:
    edges = _parse(tmp_path, "salesforce/SalesforceAmqpConfig.java", SALESFORCE)
    assert _brief(edges) == {
        ("external:rabbitmq:api.e.topic.salesforce", "api.q.salesforce.account.create",
         "account.created"),
        ("external:rabbitmq:api.e.topic.salesforce", "api.q.salesforce.account.delete",
         "account.deleted"),
    }


def test_a_declared_binding_is_not_a_listener_and_has_no_trigger(tmp_path: Path) -> None:
    path = tmp_path / "salesforce/SalesforceAmqpConfig.java"
    _parse(tmp_path, "salesforce/SalesforceAmqpConfig.java", SALESFORCE)
    _, edges = CodeParser(tmp_path).parse_file(path)
    assert not [e for e in edges if e.kind in ("CONSUMES", "HANDLES", "PRODUCES")]
    binds = [e for e in edges if e.kind == "BINDS"]
    assert all(e.extra["declaration"] is True for e in binds)
    assert all(e.extra["resolution"] == "queue_binding_class" for e in binds)


def test_exchange_and_key_constants_of_another_class_resolve(tmp_path: Path) -> None:
    edges = _parse(tmp_path, "projectadmin/ProjectAdminAmqpConfig.java", PROJECT_ADMIN)
    assert _brief(edges) == {
        ("external:rabbitmq:api.e.topic.account", "api.q.projectadmin.account.created",
         "account.provision"),
    }


def test_binding_builder_resolves_a_queue_bean_method_and_new_topic_exchange(
    tmp_path: Path,
) -> None:
    edges = _parse(tmp_path, "insight/InsightDescriptionAmqpConfig.java", INSIGHT)
    assert _brief(edges) == {
        ("external:rabbitmq:api.e.topic.attribute", "api.q.attribute.define.insight-description",
         "attribute.define"),
    }
    assert {e.extra["resolution"] for e in edges} == {"binding_builder"}


def test_a_binding_with_an_unresolvable_queue_or_exchange_is_not_written(tmp_path: Path) -> None:
    edges = _parse(tmp_path, "insight/InsightDescriptionAmqpConfig.java", INSIGHT)
    assert not [e for e in edges if e.extra["binding_key"] == "x.y"]
