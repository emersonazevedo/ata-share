# ata — transcrição de reuniões 100% local + ata automática

Grava a reunião (o áudio da call **e** o seu microfone), transcreve ao vivo e,
ao clicar em **Parar**, gera a **ata** (resumo, decisões, riscos e ações).
Tudo roda no seu computador: **nenhum áudio vai para a nuvem** e não há
assinatura paga.

> Status: protótipo funcional. Windows apenas (a captura usa WASAPI).

## Como funciona

1. Você abre a página local (`http://127.0.0.1:8756`) e clica em **Gravar**.
2. O app captura o som do sistema (Teams/Meet/Zoom...) e o microfone.
3. Um modelo de voz (Whisper `large-v3-turbo`) transcreve **durante** a reunião.
4. Ao parar, a transcrição já está pronta; uma IA local (Ollama) resume a
   reunião em blocos de ~10 min e consolida numa ata.
5. Tudo é salvo em `data/` (texto + banco SQLite com busca entre reuniões).
   Opcionalmente a ata vira uma nota no seu **Obsidian**.

## Requisitos

- Windows 10/11
- **Python 3.11 ou superior** (marque *Add Python to PATH* ao instalar)
- **Ollama** — https://ollama.com/download
- 16 GB de RAM (ideal) e ~10 GB de disco livre. Não precisa de placa de vídeo.

## Instalação (5 passos)

1. Instale o **Python 3.11+** e o **Ollama**.
2. Baixe um modelo de resumo (em um terminal):
   ```
   ollama pull qwen2.5:7b
   ```
   (~4,7 GB, melhor qualidade. Para PC mais fraco: `ollama pull llama3.2:3b`.)
3. Baixe/extraia este projeto em uma pasta, por exemplo `C:\ata`.
4. Copie `config.example.toml` para `config.toml` e, se usou o `qwen2.5:7b`,
   altere em `[ollama]`:
   ```
   model = "qwen2.5:7b"
   ```
5. Dê **duplo-clique em `ata.bat`**.
   - Na 1ª vez ele cria o ambiente e instala as dependências (alguns minutos).
   - Na 1ª transcrição baixa o modelo de voz (~1,5 GB, só uma vez).
   - O navegador abre em `http://127.0.0.1:8756`.

## Uso

1. Escolha o idioma (`pt`, `en` ou `auto`) e, se quiser, o microfone/saída.
2. **Gravar** → acompanhe a transcrição ao vivo.
3. **Parar** → a ata é gerada em segundo plano e a pasta da reunião abre.
4. Para refazer com mais precisão: botão **Reprocessar**.

Dica: use **fones de ouvido**. Com alto-falante, o microfone capta o áudio da
call e duplica falas (o app tenta remover esse eco, mas fones são melhor).

## Obsidian (opcional)

Em `config.toml`:

```toml
[obsidian]
enabled = true
vault_path = "C:/caminho/do/seu/vault"
```

Ao fim de cada ata é criada uma nota em `Reunioes/` com Objetivo, Pauta, Resumo,
Decisões, Riscos, Pontos técnicos, Ações e o transcript. Também é mantido um
`Dashboard/Dashboard.md` consolidando as reuniões.

## Privacidade

- Áudio, transcrições e atas ficam em `data/` (ignorado pelo git).
- Os modelos ficam em `models/`.
- O servidor escuta apenas em `127.0.0.1` (só o seu computador).
- A internet só é usada para baixar os modelos (Ollama e Whisper) na primeira vez.

## Problemas comuns

| Sintoma | O que fazer |
|---|---|
| "Python não encontrado" | Reinstale o Python marcando *Add to PATH* |
| Ata vazia/erro | Verifique se o Ollama está rodando e se o modelo do `config.toml` foi baixado (`ollama list`) |
| Transcrição atrasada | Feche programas pesados; ajuste `cpu_threads` em `[asr]` |
| Falas duplicadas | Use fones de ouvido |
| Reiniciar o servidor | `restart.bat` |

## Desenvolvimento

```
.venv\Scripts\python.exe -m pytest
```

Configuração completa e comentada em `config.example.toml`; qualquer valor pode
ser sobrescrito por variável de ambiente `ATA_*` (ex.: `ATA_OLLAMA_MODEL`).
