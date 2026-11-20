# Migrar de Mercurial para Git (`hgtogit`)

> [English](README.md)

Script standalone em Python 3 para migrar repositórios Mercurial (`hg`) para Git, feito para repositórios com subrepositórios (`.hgsub` / `.hgsubstate`), histórico complexo de merges e grande volume de commits.

- [Migrar de Mercurial para Git (`hgtogit`)](#migrar-de-mercurial-para-git-hgtogit)
  - [Por que usar o `hgtogit`?](#por-que-usar-o-hgtogit)
  - [Pré-requisitos e Estrutura de Diretórios](#pré-requisitos-e-estrutura-de-diretórios)
  - [Configuração](#configuração)
  - [Migrar um repositório Hg existente para Git local](#migrar-um-repositório-hg-existente-para-git-local)
  - [Retomar uma migração interrompida](#retomar-uma-migração-interrompida)
  - [Verificar com a suíte de testes sintéticos](#verificar-com-a-suíte-de-testes-sintéticos)
  - [Como funciona](#como-funciona)

---

## Por que usar o `hgtogit`?

Ferramentas como `hg-fast-export` e `hg-git` costumam falhar ou ignorar subrepositórios, travar em execuções longas no Windows ou exigir recomeçar do zero após interrupções. O `hgtogit` resolve esses pontos:

- **Mapeamento dinâmico de submódulos**: Converte os hashes Mercurial de 40 caracteres do `.hgsubstate` nos hashes Git correspondentes usando `hg2git-mapping` / `hg2git-marks`, fixando o `.gitmodules` e os gitlinks a cada commit.
- **Save-points por commit e retomada**: Salva o estado (`hg_to_git_save.json` e `last_substates_save.json`) após cada commit, permitindo pausar ou retomar sem voltar ao commit `0`.
- **Commits de merge reais (2 pais)**: Faz checkout do `p1node` e grava o `p2node` em `.git/MERGE_HEAD` antes do commit, preservando o grafo DAG original.
- **Proteção de diretórios de submódulos**: Exclui pastas de submódulos ativos no `rsync` para nunca sobrescrever os metadados `.git` internos.
- **Pronto para Windows e sem dependências externas**: Usa apenas a biblioteca padrão do Python 3 + `git`, `hg` e `rsync`, com tratamento de arquivos somente leitura (NTFS) e mensagens de commit via arquivo UTF-8.

---

## Pré-requisitos e Estrutura de Diretórios

Garanta que as ferramentas abaixo estejam disponíveis no seu `PATH`:

- `python` (Python 3.8+)
- `git`
- `hg` (Mercurial)
- `rsync` (no Windows, disponível via Git Bash / MSYS2 / WSL)

### Estrutura esperada antes de rodar

1. **Repositório Hg de origem (`REPO_HG`)**: Um clone/cópia local do seu repositório Mercurial.
2. **Submódulos já convertidos para Git (`SUBREPOS`)**: Cada subrepositório listado no `.hgsubstate` precisa já estar convertido para um repositório Git local (por exemplo, via `hg-fast-export`) contendo `.git/hg2git-mapping` e `.git/hg2git-marks`.
3. **Diretório de execução**: Execute o `hgtogit.py` a partir da **mesma pasta** onde o script está localizado (ou onde deseja que `migration_report.txt`, `hg_to_git_save.json` e `last_substates_save.json` sejam gravados).

> **Atenção:** Quando `START_COMMIT_IDX = 0`, o script apaga e recria `REPO_GIT` do zero. Nunca aponte `REPO_GIT` para uma pasta com alterações não salvas.

---

## Configuração

Edite as constantes no topo do arquivo `hgtogit.py`:

```python
REPO_HG = "/path/to/source/mercurial/repo"
REPO_GIT = "/path/to/destination/git/repo"

SUBREPOS = {
    "subrepo_1": {
        "git_path": "/path/to/subrepo_1.git",
        "git_repo_url": "/path/to/subrepo_1.git/",
        "mapping_file": "/path/to/subrepo_1.git/.git/hg2git-mapping",
        "marks_file": "/path/to/subrepo_1.git/.git/hg2git-marks",
    },
}

START_COMMIT_IDX = 0
END_COMMIT_IDX = None  # None = processa até o último commit
```

---

## Migrar um repositório Hg existente para Git local

Acesse a pasta onde está o `hgtogit.py`:

```sh
cd /caminho/para/hgtogit
python hgtogit.py
```

O script vai inicializar `REPO_GIT`, reproduzir cada revisão do Mercurial em ordem cronológica, mapear os submódulos, criar a branch `main` e rodar `git submodule update --init --recursive`.

Para conferir o histórico gerado no Git:

```sh
cd /caminho/para/repositorio/git/destino
git log --all --decorate --oneline --graph
```

---

## Retomar uma migração interrompida

Se a execução parar no meio (verifique em `migration_report.txt` ou no console qual foi o último índice concluído):

1. Mantenha os arquivos `hg_to_git_save.json` e `last_substates_save.json` no diretório de execução.
2. Altere `START_COMMIT_IDX` no `hgtogit.py` para o próximo índice a ser processado (ex.: `START_COMMIT_IDX = 1500`).
3. Execute `python hgtogit.py` novamente na **mesma pasta** onde estão os arquivos JSON de save-point.

---

## Verificar com a suíte de testes sintéticos

Você pode validar o funcionamento offline sem precisar de repositórios privados usando a suíte de testes sintéticos incluída:

```sh
python -m py_compile hgtogit.py
python -m unittest -v test_synthetic_hgtogit.py
```

---

## Como funciona

Para cada revisão de `START_COMMIT_IDX` até `END_COMMIT_IDX`, o `hgtogit.py` executa:

1. **Checkout do pai**: Faz checkout de `p1node` no Git e grava `p2node` (se houver) em `.git/MERGE_HEAD`.
2. **Atualização no Mercurial**: Remove pastas físicas de subrepos no `REPO_HG` e executa `hg update -C <rev>`.
3. **Sincronização de arquivos**: Lê `.hgsubstate` e executa `rsync` de `REPO_HG` para `REPO_GIT`, ignorando `.hg/`, `.git/` e submódulos ativos.
4. **Atualização de submódulos**: Traduz cada hash Hg do subrepo para seu hash Git via `hg2git-mapping` / `hg2git-marks` e atualiza `.gitmodules` + gitlinks.
5. **Commit e checkpoint**: Cria o commit com autor, e-mail, data e mensagem UTF-8 originais, e salva `HG_TO_GIT` e `last_substates` em disco.
