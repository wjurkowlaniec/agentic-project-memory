# Prompt instalacyjny — Agentic Project Memory

Skopiuj poniższy prompt do agenta uruchomionego w repozytorium docelowym:

```text
Zainstaluj i zarejestruj lokalny Agentic Project Memory w repozytorium pod ~/ai/research-platform.

Źródło projektu:
https://github.com/wjurkowlaniec/agentic-project-memory.git

Wykonaj wyłącznie czynności instalacyjne:
1. Sklonuj lub pobierz Agentic Project Memory do tymczasowej lokalizacji poza repozytorium docelowym.
2. Uruchom jego scripts/install.sh z --project-root ~/ai/research-platform.
3. Zainstaluj opcjonalny hook Windsurf tylko jeśli użytkownik wyraźnie poda `--with-windsurf`; hook ma wyłącznie przechwytywać oficjalne `post_cascade_response_with_transcript` do prywatnego lokalnego inboxu.
4. Zainstaluj lub zaktualizuj wyłącznie blok reguł w AGENTS.md; zachowaj całą istniejącą treść i nie modyfikuj kodu repozytorium docelowego.
5. Jeśli rejestracja już istnieje, zachowaj jej konfigurację i nie wykonuj ponownej inicjalizacji.
6. Jeśli potrzebne, użyj nazwy wynikającej z basename ścieżki albo jawnie ustaw nazwę research-platform.
7. Jeśli instalator ostrzeże, że katalog binarny uv nie jest w `PATH`, wykonaj wyświetlone polecenie `export PATH=...` tylko w bieżącej sesji; nie modyfikuj automatycznie plików powłoki.

Devin jest czytany lokalnie ze współdzielonego SQLite (domyślnie `~/.local/share/devin/cli/sessions.db`) nawet jeśli backend raportuje Windsurf. Stara historia Cascade jest dostępna tylko, gdy istnieje transcript z hooka; nie wolno skrobać cache. Importowane są wyłącznie wiadomości user/assistant. Późniejsze `pmem sync` wykonuje użytkownik ręcznie.
Bezwzględne zakazy: nie uruchamiaj pmem sync, search, preflight, inspect, rebuild ani benchmark; nie używaj --extract; nie czytaj historii rozmów, plików danych ani konfiguracji repozytorium docelowego poza tym, co jest konieczne do bezpiecznej rejestracji; nie ładuj żadnych modeli, w tym Nomic; nie uruchamiaj testów repozytorium docelowego; nie twórz commitów, nie stage'uj i nie pushuj zmian w repozytorium docelowym. Nie zmieniaj jego kodu, zależności, konfiguracji aplikacji ani danych.

Po zakończeniu pokaż:
- dokładną listę zmienionych plików/diff dotyczący wyłącznie AGENTS.md,
- wynik pmem status dla samej rejestracji (bez synchronizacji),
- wynik git status repozytorium docelowego,
- krótką informację, czy rejestracja była nowa czy zachowana,
- ręczne komendy do późniejszego testu: najpierw załaduj Nomic, potem pmem sync, pmem search i pmem preflight.

Jeżeli pojawi się niejasność lub próba dostępu do danych, zatrzymaj się i zgłoś problem zamiast zgadywać.
```

Prompt celowo przekazuje kontrolę nad późniejszym ładowaniem Nomic i synchronizacją użytkownikowi.
