# TStack no Herdr

Este fork adapta `sol-loop`, `council` e `full-stop` para as sessões existentes
do Herdr Core (`hc`). O OpenCode implementa; revisores são sessões separadas e
somente leitura. O app desktop é `herdr-app`; a TUI `herdr` é outro backend.

## Instalação neste host

Os diretórios abaixo foram confirmados no host Linux em 13/11/2026:

| Harness | Diretório de skills |
| --- | --- |
| Codex | `/home/israelvf/.codex/skills` |
| Claude Code | `/home/israelvf/.claude/skills` |
| OpenCode | `/home/israelvf/.config/opencode/skills` |
| Antigravity CLI (`agy`) | `/home/israelvf/.gemini/antigravity/skills` |

Copie as pastas `sol-loop`, `council`, `full-stop`, `herdr-messaging`,
`file-pr`, `babysit-pr` e `leaving-pr-comments` para o diretório do harness
que vai usá-las. Não sobrescreva arquivos existentes sem revisar diferenças.
As skills de review do fork não ficam instaladas automaticamente pelo clone.
Neste host, `herdr-messaging` nos quatro harnesses aponta para a mesma pasta
Codex; copiar a versão atualizada do fork para lá atualiza os quatro.

As instalações locais deste host usam esses quatro diretórios. A presença da
pasta de skills do Antigravity CLI está confirmada; a descoberta de skills pelo
`agy` depende da versão/configuração do CLI e não é inferida apenas pelo
diretório. Não inicie uma revisão até o skill aparecer e a sessão real estar
visível no `hc`.

## Modelos e papéis

`~/.config/tstack-herdr/roles.json` é configuração pessoal, fora do Git. Os
catálogos locais OpenCode, Codex e `agy` confirmaram os respectivos modelos em
13/11/2026; Claude Opus 5.5 permanece configurado pelo nome solicitado, sem
afirmar que exista uma sessão TStack ativa:

```json
{
  "implementer": {
    "harness": "opencode",
    "primary_model": "opencode/gpt-6-luna",
    "comparison_model": "opencode/deepseek-v4.1-flash"
  },
  "reviewers": {
    "sol": { "harness": "codex", "model": "gpt-6.1-sol" },
    "opus": { "harness": "claude-code", "model": "claude-opus-5-5" },
    "argon": { "harness": "agy", "model": "gemini-4-argon-high" }
  }
}
```

GPT-6 Luna é a opção principal. DeepSeek V4.1 Flash fica configurado apenas
para uma comparação futura; não o selecione automaticamente. Catálogo confirma
que um modelo existe, mas não que uma sessão Herdr o esteja usando.

Para resolver um revisor, use `hc ls --json`; confira endereço, cwd/worktree,
projeto/assunto, papel e estado com `hc get <endereço> --json`; confirme modelo
na sessão correspondente com `hc read <endereço> --lines 0`. Não pesquise ou
leia conversas de outros projetos. Se a sessão não corresponder exatamente,
marque o membro indisponível e não substitua. Não abra sessões novas para
contornar uma ausência.

O conselho usa, sem substituição:

1. Codex — GPT-6.1 Sol.
2. Claude Code — Claude Opus 5.5.
3. Antigravity CLI — Gemini 4 Argon High.

O primeiro parecer de cada membro usa a mesma base, head, critérios e evidências
e não recebe os pareceres dos outros. Os revisores são somente leitura. O
OpenCode tem a worktree de edição. `council` admite duas rodadas por padrão; a
segunda revisa apenas o diff novo e achados abertos. `sol-loop` usa um revisor
por vez e tem três rodadas por padrão. Timeout, indisponibilidade, bloqueio ou
SHA antigo não contam como aprovação. Um defeito comprovado bloqueia mesmo se
os demais aprovarem.

## Usar

- Peça `sol-loop` explicitamente para a revisão por Sol.
- Peça `council` explicitamente quando precisar de pareceres independentes dos
  três provedores.
- Invoque **`/full-stop` explicitamente** para preparar uma mudança, prová-la,
  revisar, abrir/acompanhar uma PR e parar antes do merge. Uma frase de
  encerramento “full stop” não aciona a skill. `full-stop` não chama
  `full-send`, não ativa auto-merge e nunca faz merge.

Um contrato de review é uma mensagem limpa com `conversation_id`, `round`,
remetente/destinatário e sessões, repositório/worktree, `base_sha`, `head_sha`,
critérios, evidências e a tarefa autorizada. Use SHAs completos e uma revisão
imutável. Cada finding inclui ID, severidade, arquivo/linha, impacto,
reprodução/evidência e ação mínima. O implementador registra
`FIXED`/`DECLINED`/`OPEN`/`NOT_VERIFIED`, justifica e roda checks depois das
correções.

O envio é feito por `hc send`. Guarde os recibos reais (`started`, `status`,
`turn`) e não reenvie quando a entrega for incerta: inspecione a mesma sessão.
Uma resposta tardia ou um parecer em outro SHA tem status explícito e não é
aprovação da versão atual.

`full-stop` faz prova direta antes da PR, usa `sol-loop`, `file-pr`,
`babysit-pr` e `leaving-pr-comments`, acompanha CI e reviews da PR real quando
invocado, e deixa o merge para Israel. Só um watcher acompanha cada PR.

## Registro e painel GPUI

`herdr-app/mods/tstack-herdr-review` é o mod nativo JavaScript/GPUI. A fonte de
mensagens é o registro JSONL criado pela skill, por padrão em
`~/.local/state/tstack-herdr/messages.jsonl`. Configurações e registros reais
ficam fora do clone. O logger cria diretório `0700`, arquivo `0600`, rejeita
caminho dentro de qualquer repositório Git e não permite campos de chamadas ou
saídas de ferramentas. As respostas registradas são somente o parecer final
limpo, não a tela bruta do terminal.

`hc events` contém fatos de estado/turno e não é fonte de conteúdo de conversa.
O mod não presume que esses eventos carreguem mensagens: lê apenas o JSONL
local, agrupa por conversa/rodada e mostra balões com autor, destinatário,
horário, status de entrega e ação para abrir a sessão de origem. O mod lê no
máximo o último MiB e mostra até 180 mensagens recentes.

Para instalar ou atualizar o mod no host, confira diferenças antes de
substituir arquivos locais já editados:

```sh
mkdir -p ~/.config/herdr-app/mods/tstack-herdr-review
cp -R herdr-app/mods/tstack-herdr-review/. ~/.config/herdr-app/mods/tstack-herdr-review/
herdr-app check ~/.config/herdr-app/mods/tstack-herdr-review
```

Instale o logger se ainda não existir em `~/.local/bin` (ou atualize-o após
revisar diferenças):

```sh
install -D -m 755 scripts/herdr_review_log.py ~/.local/bin/herdr-review-log
```

As preferências pessoais ficam em `~/.config/tstack-herdr/roles.json` com
permissão `0600`; o diretório usa `0700`. O conteúdo não é commitado.

Cada mensagem é um registro `kind=message`; cada recibo é um registro
`kind=delivery` com um novo `event_id`, `related_event_id` e apenas os campos
`started`, `status` e `turn` que o `hc` retornou. O restante dos metadados de
conversa e snapshot é repetido para manter correlação. Não armazene a saída
bruta do terminal.

Herdr recarrega o mod quando seus arquivos mudam. Para abrir sem alterar o
layout pessoal, use uma nova aba nativa do mod (`herdr-app commands` lista os
argumentos da versão instalada):

```sh
herdr-app do tab.new --name "TStack reviews" --mod tstack-herdr-review
```

Ao fechar a aba, a sessão e o registro permanecem. A atualização consiste em
copiar novamente os arquivos do diretório do mod acima e rodar `herdr-app
check`.

## Piloto adiado

Roteiro reservado para depois: uma correção pequena no OpenCode, revisão pelo
Codex GPT-6.1 Sol em outra sessão do Herdr, verificação dos achados pelo
OpenCode e checks finais. Esta implementação não executou esse ciclo, não
disparou um council real, não criou PR piloto e não comparou Luna com DeepSeek.
