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

As instalações locais deste host usam esses quatro diretórios. A pasta global
de skills do Antigravity CLI está presente, embora o `agy` não exponha um
comando de listagem de skills nesta versão. O fluxo Herdr inicia o CLI
diretamente, então não presume a descoberta automática da skill pelo `agy`.

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
    "opus": { "harness": "claude-code", "model": "claude-opus-5-5" }
  }
}
```

GPT-6 Luna é a opção principal. DeepSeek V4.1 Flash fica configurado apenas
para uma comparação futura; não o selecione automaticamente. Catálogo confirma
que um modelo existe, mas não que uma sessão Herdr o esteja usando.

Para resolver revisores, use `hc ls --json`; confira `where`, cwd/worktree,
projeto/assunto, papel, argv e estado com `hc get <endereço> --json`; confirme
modelo e prontidão com `hc read <endereço> --lines 0`. CWD sozinho não prova que
um harness está ausente: avalie a tarefa e o modelo, sem ler histórico de outros
projetos. Reutilize uma sessão livre somente se modelo, snapshot e acesso
somente leitura forem confirmados.

Uma invocação explícita de `council` ou `sol-loop` autoriza abrir sessões
dedicadas quando as existentes estiverem ausentes, ocupadas ou inadequadas.
Use o `hc run --bg` e os modos read-only nativos documentados em
`skills/herdr-messaging/SKILL.md`; não altere permissões de sessões existentes,
não use flags de bypass e não inicie uma CLI em modo print. Confira a sessão e
o modelo efetivo depois do launch; um diálogo de login/permissão não pode ser
contornado. Em retomadas, procure labels/nome da conversa antes de abrir
duplicatas.

O conselho usa, sem substituição:

1. Codex — GPT-6.1 Sol.
2. Claude Code — Claude Opus 5.5.

O Antigravity fica fora do conselho padrão. No teste interativo, o modo
`plan` apresentou “Approve plan” e ficou bloqueado antes do parecer final.

O primeiro parecer de cada membro usa a mesma base, head, critérios e evidências
e não recebe os pareceres dos outros. Cada resposta final aparece em Reviews
assim que chega. Ao fechar a rodada, o coordenador encaminha os pareceres aos
outros revisores como contexto do grupo. Os revisores são somente leitura. O
OpenCode tem a worktree de edição. `council` admite duas rodadas por padrão; a
segunda revisa apenas o diff novo e achados abertos. `sol-loop` usa um revisor
por vez e tem três rodadas por padrão. Timeout, indisponibilidade, bloqueio ou
SHA antigo não contam como aprovação. Um defeito comprovado bloqueia mesmo se
os demais aprovarem.

## Usar

- Peça `sol-loop` explicitamente para a revisão por Sol.
- Peça `council` explicitamente quando precisar de pareceres independentes de
  Sol e Opus.
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
local. Ele lê no máximo o último MiB e usa até 400 mensagens recentes.

O painel (título **Reviews**) funciona como um chat em grupo:

- **Lista de tasks:** uma task é uma `conversation_id`. A lista vem da mais
  recente para a mais antiga, com uma linha separadora por dia (`dd/mm/aa`),
  título legível, prévia da última mensagem, bolinhas coloridas dos
  participantes, rodada e número de mensagens.
- **Título:** campo opcional `title` da primeira mensagem; sem ele, usa o
  assunto (`Assunto:`) da primeira mensagem ou repositório e SHA curto.
- **Conversa:** um balão por mensagem, com a cor do agente, separadores de
  rodada, mensagens longas recolhidas ("Show more") e o botão "Open session"
  quando a sessão de origem está viva. Envios de grupo usam um mesmo
  `group_message_id` e viram um único balão.
- **Texto legível:** SHAs de 40 caracteres e UUIDs aparecem abreviados, caminhos
  absolutos do snapshot viram nomes curtos, e a linha `Assunto:` não se repete
  no corpo. Referências como `arquivo.js:39` são links que abrem o arquivo numa
  aba (`zed-file`); o diff não é aberto, porque os snapshots não têm `.git`.
- **Space e aba:** a task mostra onde nasceu (`space_name › tab_name`) e o link
  "go to tab" seleciona essa aba. Usa `space_id`/`tab_id` do log quando existem
  e, se não existirem, o contexto da sessão de origem enquanto ela está viva.
- **Checks:** ✓ enviado; ✓✓ cinza entregue (registro `delivery` com `sent`);
  ✓✓ azul lido; `!` entrega bloqueada. "Lido" vem de um registro `kind=read`
  ou, na falta dele, de o destinatário ter postado depois na mesma task. O
  recibo de leitura só indica leitura, não aprovação.

As cores ficam em `~/.config/tstack-herdr/roles.json`, sob `colors`. Chaves:
`implementer`, `sol`, `opus`, `gemini` e `other`; os valores são `#rrggbb`.
Chaves ausentes usam o padrão do mod.

```json
{ "colors": { "implementer": "#2563eb", "sol": "#10a37f", "opus": "#d97757" } }
```

O painel lateral é composto por `herdr-app/layouts/github-and-trunk-reviews.json`,
o mod `herdr-app/mods/tabs-with-reviews` (botão Reviews e restauração por
espaço) e `herdr-app/mods/tstack-herdr-review` (mensagens). O layout referencia
ícones nativos de Files, Changes, Chat e pane; somente os quatro SVGs de botões
de janela do Windows pertencem ao mod de abas. O preview usa o SVG do layout
nativo `github-and-trunk`.

Para instalar/atualizar esses arquivos, confira diferenças antes de substituir
versões pessoais. Isso não altera o layout selecionado; a troca para o layout
personalizado é uma ação explícita separada:

```sh
mkdir -p ~/.config/herdr-app/layouts
cp herdr-app/layouts/github-and-trunk-reviews.json ~/.config/herdr-app/layouts/
mkdir -p ~/.config/herdr-app/mods/tabs-with-reviews
cp -R herdr-app/mods/tabs-with-reviews/. ~/.config/herdr-app/mods/tabs-with-reviews/
mkdir -p ~/.config/herdr-app/mods/tstack-herdr-review
cp -R herdr-app/mods/tstack-herdr-review/. ~/.config/herdr-app/mods/tstack-herdr-review/
herdr-app check ~/.config/herdr-app/mods/tabs-with-reviews
herdr-app check ~/.config/herdr-app/mods/tstack-herdr-review
```

Depois de validar, escolha o layout manualmente com `herdr-app do layout.switch
--name github-and-trunk-reviews`. Não substitua o `main`, o `github-and-trunk`
original ou outros layouts padrão. No clone deste workflow, o layout ativo já
permanece intacto; os arquivos de configuração pessoais não fazem parte do
commit.

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

Campos opcionais de `kind=message`, todos strings não vazias: `title`,
`space_id`, `tab_id`, `space_name`, `tab_name` e `group_message_id` (UUID).
Coloque-os na primeira mensagem da task. `herdr-review-log context` imprime o
space e a aba da sessão atual a partir dos rótulos do Herdr. Um registro
`kind=read` (`event_id`, `related_event_id`, `conversation_id`, `reader`,
`created_at`) marca que uma sessão leu uma mensagem:

```sh
herdr-review-log context
herdr-review-log read --conversation <uuid> --event <event_id da mensagem lida>
```

Herdr recarrega os mods quando seus arquivos mudam. Se não usar o layout
integrado, o painel ainda pode ser aberto sem alterar o layout pessoal em uma
aba nativa (`herdr-app commands` lista os argumentos da versão instalada):

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
