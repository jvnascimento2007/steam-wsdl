# steam-wsdl

Baixador de mods do **Steam Workshop** para a linha de comando, com downloads concorrentes, retomada de downloads interrompidos e interface rica no terminal.

Você passa links (de um item, de vários ou de um arquivo `.txt`) e o programa baixa tudo para uma pasta, em paralelo, mostrando o progresso de cada item.

## Sumário

- [Para que serve](#para-que-serve)
- [Como funciona](#como-funciona)
- [Instalação](#instalação)
- [Uso](#uso)
- [Opções](#opções)
- [Formato do arquivo de links](#formato-do-arquivo-de-links)
- [Estrutura da pasta de saída](#estrutura-da-pasta-de-saída)
- [Códigos de saída](#códigos-de-saída)
- [Solução de problemas](#solução-de-problemas)
- [Desenvolvimento](#desenvolvimento)

## Para que serve

- Baixar vários mods de uma vez a partir de uma lista de links.
- Baixar um único item pelo link (ou pelo ID).
- Expandir **coleções** do Workshop e baixar todos os itens que elas contêm.
- Retomar downloads que caíram e tentar de novo automaticamente em falhas de rede.
- Gerar uma lista dos links que falharam, para tentar de novo depois.

## Como funciona

O programa consulta a API pública da Steam (sem chave) para obter título, jogo e tamanho de cada item e escolhe um de dois métodos:

| Método | Quando é usado | Observações |
|---|---|---|
| **Direto** | O item expõe uma URL de download direto (comum em jogos mais antigos, como Garry's Mod). | Usa só HTTP, com vários downloads em paralelo (`-w`), retomada via `Range` e novas tentativas com espera crescente. |
| **`steamcmd`** | O item não tem URL direta (comum em jogos modernos). | Exige o `steamcmd` instalado. Cada sessão faz **um único login** e baixa vários itens em sequência; várias sessões rodam em paralelo. |

Se o download direto falhar e o `steamcmd` estiver disponível, o item cai automaticamente para o `steamcmd`.

> **Atenção:** alguns jogos só permitem baixar itens do Workshop com uma conta que possua o jogo. Nesses casos o login anônimo do `steamcmd` falha, e o item aparece como falha com uma mensagem explicando o motivo (veja `--steam-user`).

## Instalação

### Requisitos

- Python 3.9 ou mais novo
- [uv](https://docs.astral.sh/uv/) (recomendado)
- `steamcmd` (opcional, mas necessário para itens sem download direto)

### Método recomendado: `uv tool install` em todo o sistema

Na pasta do projeto (a que contém o `pyproject.toml`), rode:

```bash
sudo UV_TOOL_DIR=/opt/steam-wsdl UV_TOOL_BIN_DIR=/usr/local/bin "$(which uv)" tool install .
```

Isso instala o programa em um ambiente isolado em `/opt/steam-wsdl` e cria o comando `steam-wsdl` em `/usr/local/bin`, que já está no PATH. Depois, em qualquer pasta:

```bash
steam-wsdl --help
```

Pontos importantes sobre esse comando:

- `"$(which uv)"` é resolvido **antes** do `sudo`, então funciona mesmo que o `uv` não esteja no PATH do root.
- O `uv tool install .` **copia** o código para o ambiente. Você pode mover ou apagar a pasta do projeto sem afetar o comando instalado.
- O ambiente é criado como root e usa o Python que o `uv` escolher. Se o `uv` baixar um Python para a pasta do root, outros usuários podem não conseguir executar o comando. Para usar o Python do sistema, adicione `--python /usr/bin/python3` (precisa atender ao `requires-python` do `pyproject.toml`):

  ```bash
  sudo UV_TOOL_DIR=/opt/steam-wsdl UV_TOOL_BIN_DIR=/usr/local/bin "$(which uv)" tool install --python /usr/bin/python3 .
  ```

**Atualizar** (depois de alterar o código):

```bash
sudo UV_TOOL_DIR=/opt/steam-wsdl UV_TOOL_BIN_DIR=/usr/local/bin "$(which uv)" tool install --force .
```

**Desinstalar:**

```bash
sudo UV_TOOL_DIR=/opt/steam-wsdl UV_TOOL_BIN_DIR=/usr/local/bin "$(which uv)" tool uninstall steam-wsdl
```

### Instalação só para o seu usuário

Sem `sudo`, o comando vai para `~/.local/bin`:

```bash
uv tool install .
uv tool update-shell   # se o comando não for encontrado, depois abra um novo terminal
```

### Sem instalar nada

```bash
uv run steam-wsdl -u "https://steamcommunity.com/sharedfiles/filedetails/?id=123456789"
```

### Instalando o `steamcmd`

Necessário apenas para itens sem download direto.

- **Debian/Ubuntu:** `sudo apt install steamcmd` (pode exigir habilitar o repositório `multiverse` e a arquitetura `i386`).
- **Arch Linux:** pacote `steamcmd` no AUR.
- **Outros sistemas / Windows / macOS:** siga as instruções da Valve em <https://developer.valvesoftware.com/wiki/SteamCMD>.

O programa procura `steamcmd`, `steamcmd.sh` ou `steamcmd.exe` no PATH. Se estiver em outro lugar, use `--steamcmd CAMINHO`.

## Uso

Um único item:

```bash
steam-wsdl -u "https://steamcommunity.com/sharedfiles/filedetails/?id=123456789" -o ./mods
```

O argumento `-u` também aceita só o ID numérico e pode ser repetido:

```bash
steam-wsdl -u 123456789 -u 987654321 -o ./mods
```

Uma lista em arquivo (um link por linha):

```bash
steam-wsdl -f mods.txt -o ./mods -w 8
```

Combinando as duas formas e guardando as falhas para tentar de novo:

```bash
steam-wsdl -f mods.txt -u 987654321 --failed-file falhas.txt
steam-wsdl -f falhas.txt            # tenta de novo só os que falharam
```

Itens já baixados são **pulados**, então rodar o mesmo comando de novo é seguro. Use `--overwrite` para baixar novamente.

Interromper com **Ctrl+C** encerra com calma. Os arquivos parciais (`.part`) são mantidos e retomados na próxima execução.

## Opções

### Entrada (pelo menos uma é obrigatória)

| Opção | Descrição |
|---|---|
| `-u`, `--url LINK` | Link (ou ID) de um item do Workshop. Pode ser repetido. |
| `-f`, `--file ARQUIVO.txt` | Arquivo de texto com um link por linha. |

### Geral

| Opção | Padrão | Descrição |
|---|---|---|
| `-o`, `--output PASTA` | `workshop_downloads` | Pasta de saída (criada se não existir). |
| `-w`, `--workers N` | `4` | Downloads simultâneos, de 1 a 32. Também define o número de sessões do `steamcmd`. |
| `-r`, `--retries N` | `3` | Tentativas extras em falhas de rede, de 0 a 20. |
| `-t`, `--timeout SEG` | `30` | Timeout de rede em segundos, de 5 a 600. |
| `--overwrite` | desligado | Baixa de novo itens que já existem. |
| `--failed-file ARQUIVO.txt` | — | Grava neste arquivo os links que falharam. |
| `-V`, `--version` | — | Mostra a versão. |
| `-h`, `--help` | — | Mostra a ajuda. |

### `steamcmd`

| Opção | Padrão | Descrição |
|---|---|---|
| `--steamcmd CAMINHO` | busca no PATH | Caminho do executável do `steamcmd`. |
| `--no-steamcmd` | desligado | Nunca usa o `steamcmd`. Itens sem download direto falham. |
| `--force-steamcmd` | desligado | Usa o `steamcmd` para todos os itens. |
| `--steam-user USUARIO` | `anonymous` | Conta Steam com login **já salvo** no `steamcmd`. Força 1 sessão, porque a mesma conta não pode logar em paralelo. |
| `--steamcmd-sessions N` | igual a `-w` | Sessões simultâneas do `steamcmd` (1 a 16). Cada sessão faz um único login e baixa vários itens em sequência. |
| `--steamcmd-timeout SEG` | `900` | Encerra uma sessão após SEG segundos **sem atividade** (30 a 7200). |

Dica sobre desempenho: cada sessão do `steamcmd` baixa **um item de cada vez**, então o paralelismo é igual ao número de sessões. Se aparecerem falhas estranhas com muitas sessões, reduza com `--steamcmd-sessions 2` ou `3`.

## Formato do arquivo de links

Uma entrada por linha. Aceita links completos ou IDs numéricos. Linhas vazias e linhas que começam com `#` ou `;` são ignoradas, e só a primeira palavra de cada linha é usada (o resto pode servir de anotação).

```text
# Mods do servidor
https://steamcommunity.com/sharedfiles/filedetails/?id=123456789
https://steamcommunity.com/workshop/filedetails/?id=987654321
555444333        # só o ID também funciona
; coleção inteira
https://steamcommunity.com/sharedfiles/filedetails/?id=111222333
```

Links inválidos não interrompem a execução: o programa avisa o número da linha e continua com os demais. O arquivo deve estar em UTF-8.

## Estrutura da pasta de saída

Os itens são organizados pelo ID do jogo (App ID):

```text
workshop_downloads/
└── 108600/                                  # App ID do jogo
    ├── Meu Mod [123456789].gma              # download direto: um arquivo
    └── Outro Mod [987654321]/               # via steamcmd: uma pasta
        └── ...
```

Caracteres inválidos em nomes de arquivo são trocados por `_`.

## Códigos de saída

| Código | Significado |
|---|---|
| `0` | Tudo certo. |
| `1` | Pelo menos um item falhou (ou houve erro ao consultar o Workshop). |
| `2` | Erro de uso: argumentos inválidos, arquivo de links ausente ou ilegível, pasta de saída sem permissão de escrita. |
| `130` | Interrompido com Ctrl+C. |

Isso permite usar o programa em scripts:

```bash
steam-wsdl -f mods.txt -o ./mods || echo "alguns itens falharam"
```

## Solução de problemas

| Sintoma | Causa provável e solução |
|---|---|
| `no direct download URL ... install steamcmd` | O item não tem download direto e o `steamcmd` não foi encontrado. Instale-o ou use `--steamcmd CAMINHO`. |
| `Steam requires an account that owns the game` | O jogo não permite download anônimo. Faça login uma vez no `steamcmd` com a sua conta e use `--steam-user SEU_USUARIO`. |
| `item not found, removed, or private` | O item foi apagado ou não é público. |
| `steamcmd was idle for N s` | A sessão travou ou a conexão caiu. Rode de novo (itens prontos são pulados) ou aumente `--steamcmd-timeout`. |
| Falhas aleatórias com muitas sessões | Reduza `--steamcmd-sessions`. |
| `steam-wsdl: command not found` | Confira se `/usr/local/bin` (ou `~/.local/bin`) está no PATH. Com `uv tool install` sem `sudo`, rode `uv tool update-shell`. |
| `ModuleNotFoundError: No module named 'rich'` | Ambiente sem as dependências. Reinstale com `uv tool install --force .`. |
| `could not use the output directory` | Sem permissão de escrita na pasta indicada em `-o`. |

## Desenvolvimento

A organização dos módulos e as instruções de teste estão em [Arquitetura](docs/architecture.md).

```bash
git clone <URL-DO-REPOSITORIO>
cd steam-wsdl
uv sync                          # cria o .venv com as dependências
uv run steam-wsdl --help         # roda direto do código-fonte
uv tool install -e .             # opcional: comando que reflete as edições na hora
```

Dependências de execução: [`requests`](https://requests.readthedocs.io/) e [`rich`](https://rich.readthedocs.io/).

## Aviso

Baixe apenas conteúdo que você tenha direito de usar e respeite os termos de uso da Steam e as licenças de cada mod.
