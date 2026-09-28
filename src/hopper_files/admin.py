"""Local administrative commands. Passwords are read with getpass, never argv."""

from __future__ import annotations

import sys
import time
from argparse import ArgumentParser
from getpass import getpass
from pathlib import Path

from hopper_files.config import (
    ConfigError,
    load_config,
    require_service_account,
    validate_service_identity,
)
from hopper_files.credentials import PasswordTooLong, change_password, password_byte_length
from hopper_files.kdf import PASSWORD_MAX_BYTES, KdfUnavailable, prove_kdf_runtime
from hopper_files.lifecycle import (
    LifecycleError,
    SYSTEM_LAYOUT,
    install_shared,
    migrate_release,
    register_instance,
    return_release,
    remove_instance,
    rollback_release,
    uninstall_shared,
    update_instance,
    update_release,
    verify_installed_release,
)
from hopper_files.migration import require_current_format
from hopper_files.state import KdfBusy, StateError, ensure_initialized, initialize_state
from hopper_files.trash import TrashError, TrashStore


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(prog="hopper-files-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("set-password", "check", "purge-trash"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
    install = commands.add_parser("install-shared")
    install.add_argument("--artifact", required=True, type=Path)
    install.add_argument("--sha256", required=True)
    update = commands.add_parser("update-release")
    update.add_argument("--artifact", required=True, type=Path)
    update.add_argument("--sha256", required=True)
    rollback = commands.add_parser("rollback-release")
    rollback.add_argument("--release-id", required=True)
    verify = commands.add_parser("verify-release")
    verify.add_argument("--release-id", required=True)
    register = commands.add_parser("register-instance")
    register.add_argument("--config", required=True, type=Path)
    update_instance_command = commands.add_parser("update-instance")
    update_instance_command.add_argument("--config", required=True, type=Path)
    remove = commands.add_parser("remove-instance")
    remove.add_argument("--instance-id", required=True)
    commands.add_parser("uninstall-shared")
    migrate = commands.add_parser("migrate-release")
    migrate.add_argument("--artifact", required=True, type=Path)
    migrate.add_argument("--sha256", required=True)
    migrate.add_argument("--yes", action="store_true")
    back = commands.add_parser("return-release")
    back.add_argument("--release-id", required=True)
    back.add_argument("--yes", action="store_true")
    args, extra = parser.parse_known_args(argv)
    if extra:
        print("Argumentos não reconhecidos.", file=sys.stderr)
        return 2
    if args.command == "install-shared":
        return _lifecycle_result(lambda: print(f"Instalação compartilhada ativa: {install_shared(args.artifact, args.sha256)}"))
    if args.command == "update-release":
        return _lifecycle_result(
            lambda: _print_release_change(update_release(args.artifact, args.sha256), "Atualização concluída")
        )
    if args.command == "rollback-release":
        return _lifecycle_result(
            lambda: _print_release_change(rollback_release(args.release_id), "Rollback concluído")
        )
    if args.command == "verify-release":
        return _lifecycle_result(
            lambda: print(
                f"Release {args.release_id} verificada: tree "
                f"{verify_installed_release(SYSTEM_LAYOUT, args.release_id)['sourceTree']}"
            )
        )
    if args.command == "register-instance":
        return _lifecycle_result(lambda: print(f"Instância registrada e ativa: {register_instance(args.config)}"))
    if args.command == "update-instance":
        return _lifecycle_result(lambda: print(f"Configuração aplicada à instância: {update_instance(args.config)}"))
    if args.command == "remove-instance":
        return _lifecycle_result(
            lambda: _print_removal(remove_instance(args.instance_id), args.instance_id)
        )
    if args.command == "migrate-release":
        return _lifecycle_result(
            lambda: _print_migration(
                migrate_release(args.artifact, args.sha256, confirm=_confirmation(args.yes)), "Migração concluída"
            )
        )
    if args.command == "return-release":
        return _lifecycle_result(
            lambda: _print_migration(return_release(args.release_id, confirm=_confirmation(args.yes)), "Retorno concluído")
        )
    if args.command == "uninstall-shared":
        return _lifecycle_result(lambda: _print_uninstall(uninstall_shared()))
    if args.command == "check":
        return check(args.config)
    if args.command == "purge-trash":
        return purge_trash(args.config)
    return set_password(args.config)


def _lifecycle_result(operation) -> int:
    try:
        operation()
    except LifecycleError as exc:
        print(f"Operação de ciclo de vida recusada: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Operação de ciclo de vida incompleta: {exc}", file=sys.stderr)
        return 1
    return 0


def _confirmation(already_confirmed: bool):
    """Show the effective choices and ask; --yes records a confirmation already given."""

    def confirm(summary: str) -> bool:
        print(summary)
        if already_confirmed:
            print("Confirmado por --yes.")
            return True
        return input("Confirmar? Digite sim: ").strip().lower() == "sim"

    return confirm


def _print_migration(result: dict[str, object], label: str) -> None:
    print(f"{label}: {result['previous']} -> {result['current']}")
    reports = result.get("reports")
    if isinstance(reports, dict):
        for iid, report in sorted(reports.items()):
            counts = report.get("counts", {}) if isinstance(report, dict) else {}
            summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "nenhum registro"
            backup = report.get("backup") if isinstance(report, dict) else None
            print(f"- {iid}: {summary}")
            if backup:
                print(f"  relatório e cópias: {backup}")


def _print_release_change(change: tuple[str, str], label: str) -> None:
    previous, current = change
    print(f"{label}: {previous} -> {current}")


def _print_removal(paths: tuple[Path, Path, Path], iid: str) -> None:
    config, state, unit = paths
    print(f"Instância removida: {iid}; unidade {unit} desativada.")
    print(f"Configuração, estado e documentos preservados: {config}; {state}.")


def _print_uninstall(result: tuple[int, tuple[Path, ...]]) -> None:
    count, configs = result
    print(f"Aplicação compartilhada desinstalada; {count} instância(s) removida(s).")
    print("Configurações, estados e documentos foram preservados.")
    for path in configs:
        print(f"Configuração preservada: {path}")


def check(path: Path) -> int:
    try:
        config = load_config(path)
        require_service_account(config)
        ensure_initialized(config.state_directory, config.instance_id)
        require_current_format(config.state_directory)
        validate_service_identity(config)
        prove_kdf_runtime()
    except (ConfigError, StateError, KdfUnavailable) as exc:
        print(f"A instância não passou na verificação: {exc}", file=sys.stderr)
        return 1
    print(
        f"Instância {config.instance_id} válida. "
        f"Modo {config.access_mode}. "
        f"Escuta {config.bind_host}:{config.bind_port}. "
        f"Origem {config.origin}. "
        f"Fuso {config.time_zone}."
    )
    return 0


def purge_trash(path: Path) -> int:
    """Delete only trash entries whose protected age is at least 30 days.

    The clock is the server clock. Quarantined metadata and entries younger
    than 30 days stay in place. This command is not an early permanent delete.
    """
    try:
        config = load_config(path)
        require_service_account(config)
        ensure_initialized(config.state_directory, config.instance_id)
        removed = TrashStore(config.state_directory, config.instance_id).purge(time.time())
    except (ConfigError, StateError, TrashError, OSError) as exc:
        print(f"A lixeira não pôde ser purgada: {exc}", file=sys.stderr)
        return 1
    if removed == 1:
        print("1 entrada vencida foi removida.")
    else:
        print(f"{removed} entradas vencidas foram removidas.")
    return 0


def set_password(path: Path) -> int:
    try:
        config = load_config(path)
        require_service_account(config)
        initialize_state(config.state_directory, config.instance_id)
        validate_service_identity(config)
    except (ConfigError, StateError, OSError) as exc:
        # The cause helps whoever is installing it: for example, a state directory the account cannot create.
        print(f"A configuração da instância foi recusada: {exc}", file=sys.stderr)
        return 1
    first = getpass("Nova senha: ")
    second = getpass("Confirme a nova senha: ")
    if first != second:
        print("As senhas não coincidem.", file=sys.stderr)
        return 2
    if not first:
        # The login screen requires a password; an empty one would leave the instance without access.
        print("A senha não pode ficar vazia.", file=sys.stderr)
        return 2
    try:
        if password_byte_length(first) > PASSWORD_MAX_BYTES:
            raise PasswordTooLong("password exceeds 1024 bytes")
        change_password(config.state_directory, first, instance_id=config.instance_id)
    except PasswordTooLong:
        print("A senha excede o limite.", file=sys.stderr)
        return 2
    except KdfBusy:
        print("O verificador está ocupado.", file=sys.stderr)
        return 3
    try:
        print("Senha atualizada.")
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
