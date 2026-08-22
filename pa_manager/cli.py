from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from pa_manager.config import ConfigurationError, CredentialResolver, Settings, load_accounts
from pa_manager.db import Repository
from pa_manager.logging_setup import configure_logging
from pa_manager.models import Action
from pa_manager.service import ActionNotAllowed, ManagerService, OperationInProgress


def _scope(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="processa todas as contas")
    group.add_argument("--account", help="id da conta no accounts.yaml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pa-manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate-config", help="valida YAML e presença dos secrets")
    verify = subparsers.add_parser("verify", help="consulta status e health check")
    _scope(verify)
    renew = subparsers.add_parser("renew", help="simula ou executa renovação")
    _scope(renew)
    renew.add_argument(
        "--real",
        action="store_true",
        help="executa a ação real; sem esta opção a execução sempre será DRY RUN",
    )
    reload_parser = subparsers.add_parser("reload", help="executa reload")
    _scope(reload_parser)
    parser.add_argument("--json", action="store_true", help="imprime relatório JSON")
    return parser


def _ids(args: argparse.Namespace) -> list[str] | None:
    return None if args.all else [args.account]


def _print_report(data: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    run = data["run"]
    print(
        f"{run['status'].upper()}: {run['total']} processados, "
        f"{run['succeeded']} sucessos, {run['warnings']} atenções, {run['failed']} falhas"
    )
    for item in data["results"]:
        print(f"- {item['domain']}: {item['result_status']} — {item['message']}")


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    accounts = load_accounts(settings.accounts_file)
    resolver = CredentialResolver()
    if args.command == "validate-config":
        configured = sum(resolver.configured(account) for account in accounts)
        print(f"Configuração válida: {len(accounts)} contas; {configured} com credencial presente.")
        return 0

    repository = Repository(settings.db_path)
    repository.initialize()
    service = ManagerService(
        settings,
        repository,
        accounts,
        credential_resolver=resolver,
    )
    if args.command == "verify":
        action = Action.VERIFY
        dry_run = None
    elif args.command == "renew":
        action = Action.RENEW
        dry_run = not args.real
    else:
        action = Action.RELOAD
        dry_run = None
    run_id = await service.run_now(action, account_ids=_ids(args), dry_run=dry_run)
    data = repository.get_run(run_id)
    if data is None:
        print("Relatório não encontrado.", file=sys.stderr)
        return 2
    _print_report(data, args.json)
    return 1 if data["run"]["failed"] else 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        settings = Settings()
        configure_logging(settings.log_level)
        exit_code = asyncio.run(_run(args, settings))
    except (ConfigurationError, ActionNotAllowed, OperationInProgress) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        exit_code = 2
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
