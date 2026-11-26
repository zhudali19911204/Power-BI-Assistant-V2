# Power BI Assistant user guide

Use the assistant with a report open in Power BI Desktop to write DAX, edit Power Query transformations and analyze live data. Open **User guide** in the sidebar to read this guide offline.

## Ask your first question

1. Open a PBIX file in Power BI Desktop and wait for the model to load.
2. Open **Model services** in the lower left. Select **Add model**, enter the connection details and use **Save and test**. After a successful test, choose **Save and use**.
3. Select **Connect Power BI** in the upper right. Match the report name and file path. The button changes to **Desktop connected** and the model panel shows the report’s tables, fields and measures.
4. If a real report has no usable business definitions, DAX Assistant and Data Analysis open **Model semantics**. Build a dictionary or enter known definitions, then review the metrics you need.
5. In **DAX Assistant**, click an existing measure in the model panel and ask “How does this measure respond to a date filter?” Press Enter to send. Shift+Enter starts a new line.

Select **中文** in the upper right to switch to Chinese, or **English** to switch back. The assistant remembers your selection on this computer. New questions use the selected reply language unless you request another language. You can switch with a draft or generation in progress.

You can write your first question and attach a screenshot before connecting. After you select your first report, review the draft and send it. To explore without a report, choose **Try demo model** in the connection dialog. Generating replies requires an AI service. Live queries, calibration and model writes require Desktop.

Continue to [Write DAX](#write-dax), [Transform data with Power Query](#transform-data-with-power-query) or [Analyze and verify results](#analyze-and-verify-results).

## Install and start

The desktop app runs on Windows 10/11 x64 with Edge WebView2 Runtime. Install Power BI Desktop on the same computer. Run the installer, copy the machine code on the licensing screen and obtain a matching CD-KEY from your publisher. Launch Power BI Assistant from the Start menu. The default program directory is `%LOCALAPPDATA%\Programs\PowerBIAssistant`.

Permanent and seven-day trial keys are machine-specific. The trial runs from issuance and its expiry is shown in UTC. To move to a permanent license, close the assistant and run the installer with the permanent key for that computer.

### Updates

Users of 2.0.12 and earlier can install 2.0.13 over the existing installation to enable online updates. Existing chats, dictionaries, model settings and license expiry carry over. Installed versions with online updates check at startup and every six hours. Select **Check for updates** to check manually. **Update and restart** downloads and verifies the installer. Finish active tasks and save unsent drafts and images before restarting. You can choose **Later** during download.

### Run from source

From PowerShell in the project directory, using Python 3.11 or newer (the project has been tested with 3.12):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.\.venv\Scripts\python.exe desktop.py
```

For subsequent starts, double-click `start.bat`. To preview in your browser, run `.\.venv\Scripts\python.exe desktop.py --browser --port 8765`. Press Ctrl+C in the terminal to stop the preview.

## Configure a model service

Open **Model services** from the sidebar or the model selector below the input. Select **Add model** and give the configuration a recognizable display name.

| Setting | What to enter |
| --- | --- |
| API protocol | Select OpenAI-compatible or Anthropic Messages to match your provider. |
| API base URL | Use the provider’s base URL, often ending in `/v1`. The official Anthropic service accepts a blank URL. |
| Model name / ID | Enter the deployed model ID. For screenshots, choose a model that accepts image input. |
| API key | Enter the credential for this service. An unauthenticated internal service can use `local`. When editing, a blank key keeps the saved credential. |

**Save only** stores the configuration. **Save and use** selects it for subsequent requests. **Save and test** sends a real test request. An in-progress response keeps the model selected when it started.

New configurations default to low reasoning effort. Supported models provide low/high effort or a reasoning switch. The model card shows the effective setting.

| Advanced setting | Default | Range |
| --- | --- | --- |
| Output tokens per request | 16,000 | 256–131,072, within the provider’s limit |
| Idle timeout | 90 seconds | 30–600 seconds |
| Total generation timeout | 900 seconds | 180–7,200 seconds, at least the idle timeout |

If output is cut short, narrow the question or raise the output limit. If no new text or reasoning arrives, check the service connection and load.

## Connect and sync a report

The connection dialog lists local Desktop instances. Check the file path when reports have similar names. **Sync model** is beside the model panel heading.

| State | What to do |
| --- | --- |
| No open instances | Open the report in Desktop, wait for it to load and select **Scan again**. |
| Checking connection, Reconnecting or Waiting for Desktop | Open the original report and allow it to reconnect. Use the status button to scan again if needed. |
| Select report | Choose the intended report from the matching instances. |
| Sync required | Sync after changes to columns, measures, relationships or queries. Earlier replies are marked as predating the sync. |
| Snapshot released | History remains available. Open the corresponding report and sync to query it again. |

The assistant keeps the full schema of the most recently connected or synced Desktop model. Switching reports releases the previous schema and value cache. You can still read its chats, query results, templates and dictionaries. To continue working on the same report in the current chat, use **Sync model**. Selecting a different report in an existing chat starts a new chat.

## Find fields and values

Under **Model context**, the **Schema** tab lists tables, columns and measures. Click an object to insert its reference at the cursor. **Relationships** shows joined columns, filter direction and active status. **Queries** lists available M queries.

Search by technical name or by a confirmed business name or alias in the dictionary. A match displays the business name alongside its model reference. In a narrow window, **Model fields** opens the field panel.

Click the eye beside a column to inspect distinct values, counts, blanks and samples. The preview shows up to 50 distinct values by default and supports search. It covers the whole model. To reproduce a report visual, specify the date, organization and other slicer selections in your question.

## Build and transfer a business dictionary

Open **Model semantics** and check the report and AI service. **Build model definitions** sends relevant structure, formulas and descriptions to the service. Definitions are saved in batches. A business overview follows. Review the metrics you intend to use first.

| View | What to record |
| --- | --- |
| Business overview | Report purpose, data grain, time definitions and limitations |
| Business terms and conventions | Business terminology and defaults to apply when a question omits a condition |
| Object dictionary | Business names, definitions, units, aliases and applicable scope for tables, columns and measures |

A business term can consist of a name and meaning when its scope is the entire model. An analysis convention needs a linked object or a filter mapping. For example, link the posting-month field to a convention that uses posting month when the question omits a period. Older conventions without mappings remain available for review.

Generated definitions are suggestions. Check field values and confirm the intended meaning. For codes such as `0257`, keep the original data type, field and leading zeros. If the same value occurs in several fields, use the clarification card to specify its meaning.

After syncing a changed model, review the change list and update pending objects. **Rebuild all definitions** refreshes suggestions and preserves valid manual definitions. Under **Manage dictionaries**, you can preview a saved version, compare changes and link it. The connection dialog also provides access to saved dictionaries for browsing, renaming or deletion.

Use **Export semantics** on the source model, then **Import semantics** on the target. Select the JSON file (up to 64 MB), inspect the merge preview and confirm. Compatible imports use definitions from the file. Locally completed convention mappings take precedence over incomplete ones. Schema differences protect current manual definitions, skip missing objects and flag affected entries for review.

## Write DAX

Specify whether you want a measure, calculated column or calculated table. Include the relevant fields, period, units and blank handling. For example, “Use my existing gross profit and sales measures to calculate gross margin. Return BLANK() when sales are zero and preserve report filters.” You can also start with **Create measure**, **Explain DAX** or **Optimize DAX**.

| Validation result | Meaning |
| --- | --- |
| Static checks passed · No live run | Structure and model references were checked. Connect Desktop and run the checks again for live validation. |
| Verified in Desktop | Inspect the actual result. Measures generated together are tested with their temporary dependencies. |
| Desktop row-context sample passed | A calculated column was sampled on up to 50 rows. The full calculation runs when you confirm the write. |
| Validation failed | Review the error, calibration difference or subsequent revision. Automatic repair allows up to two attempts. |

Use **Expand code**, **Wrap lines** or **Copy** in a code block. Copy preserves the original code and comments.

### Calibrate and write

1. With a live Desktop model, choose **Calibrate** above the input. Combine up to ten equality conditions and enter an expected result and absolute tolerance. Leave filters empty to check the entire model.
2. Choose **Apply to future responses** and send your request. Calibration tests the **last measure** in the response. For a known margin of 25%, enter its underlying value, `0.25`. Compare expected and actual values in the result card.
3. Save a copy of the report. After a successful live check, select **Review and write to model**. Review the expression, target name and table. Overwriting a measure requires explicit confirmation. Calculated columns and tables are created under new names.
4. Write a base measure before writing measures that depend on it. Check the result in Desktop and press Ctrl+S to save the PBIX.

## Transform data with Power Query

1. Connect a report that contains M queries, open **Power Query** and select the target below the input. You can also open a query in the model panel and select **Use in Power Query**. Changing the target in an existing chat opens a new chat.
2. Describe the change. For example, “Keep the source, trim the customer names, convert Order Date to date and identify rows that fail conversion.”
3. Review the static checks and M script. In the matching query in Desktop, keep a copy of the original and paste the new script into Advanced Editor. Check the source, data types, errors and row count, then choose **Close & Apply**.

## Analyze and verify results

In **Data Analysis**, name the metric, date range, comparison, grouping and units. For example, “Compare January–May 2026 sales with the same posting months last year. List the three product categories with the largest changes.” Include page filters when reproducing a visual. The assistant runs read-only DAX against the connected Desktop model.

- Use a clarification card to choose an interpretation or enter your own.
- Expand a reference such as `[Q1]` to inspect its DAX, filters and actual results. Field checks such as `[F1]` show values and samples. Check the denominator before interpreting a contribution percentage.
- Tables preview the first 20 rows. **Expand table** and **Download results as CSV** show the saved result, up to 200 rows and approximately 24 KB per query. Narrow or aggregate a truncated result. A query can return up to 30 columns.
- A turn can run up to four batches, 12 business queries and 18 execution attempts including repairs. If a limit is reached, review which questions the available evidence answers.

### Save and rerun an analysis template

Select **Save as analysis template** beneath a completed analysis. The task contains the question from that turn and the scope of the queries that succeeded in that reply. You can edit the suggested name before saving. You can also choose **New template** in **Analysis templates**. Include any definitions that depend on screenshots in the task.

Under **Run parameters**, each row has one parameter and its default. Saving from an analysis finds reusable values automatically. Select the values you want to change in future runs and edit their defaults in the same rows. For a new task, use **Find values in task** or select text and add a **Custom parameter**.

Choose **Save template** to connect the parameters to the saved queries. If a value appears in several filter fields, select the field in its parameter row and save again. Parameters entered directly in DAX under **Query code (advanced)** also appear in the same list.

Connect the template’s report and select **Run again**. The fields open with the saved defaults. Select **Run analysis** to use them, or edit a field first to change its value for this run. A field without a default starts with its last value, or requires an entry if it has never been used. **Run history** records the version, parameters and results in a separate chat for each run.

Each run sends the same DAX to Desktop with its own values. The assistant analyzes the rows returned by that run. A manually created template can include DAX or prepare it on the first run. Older templates reuse successful queries from the source analysis or a previous run of the same revision when available.

## Chats, images and exports

| Task | Action |
| --- | --- |
| Start or switch chats | Ctrl+K opens a new chat. Search recent chats by title. Each workspace keeps its draft and an active generation continues in its own chat. |
| Stop generation | Use the stop button beside Send. Text and results already received remain in the chat. Exiting the app also stops its local service. |
| Add images | Use **Images**, drag files in or paste with Ctrl+V. **Screenshot** opens Windows Snipping Tool. Paste the capture into the input. A message supports up to four PNG, JPEG or WebP files, each at most 8 MB. |
| Resize panels | Drag a sidebar divider or double-click it to reset its width. Use **Model fields** to open the model panel in narrow windows. |
| Export | Use the download icon above the chat to save Markdown with replies, code, validation, saved results and embedded images. Each query table also offers CSV. |
| Delete history | Select the delete button on a recent chat and confirm. Manage objects already written to the report separately in Desktop. |

### History compression

Open **Context** above the chat. Usage shows the higher of message and character utilization. Data Analysis allows 16 messages and 40,000 characters. DAX and Power Query allow 20 messages and 60,000 characters. A summary counts toward both limits. The current input, attachments, schema and current query results are accounted for separately.

By default, history is compressed locally before the next send once usage reaches 80%. You can turn this off per chat or choose **Compress now** when enough history has accumulated. Compression favors the two most recent exchanges, summarizing an oversized exchange when necessary. **View history summary** shows earlier requests, confirmed definitions and query references. Full messages, attachments and saved results remain available for reading and export.

## Back up and restore

| Installation | Default user data directory |
| --- | --- |
| Installed app | `%LOCALAPPDATA%\PowerBIAssistant` |
| Source checkout | `data\` in the project directory |
| Custom location | The directory specified by `PBI_DATA_DIR` before startup |

Close the assistant and copy the entire user data directory. Include `assistant.sqlite3`, which contains chats, images, dictionaries, settings and execution records, and `secret.key`, which decrypts stored service keys. Back up the destination before restoring. Reopen the app, verify the settings and reconnect the report. Changing `PBI_DATA_DIR` selects a location. Copy the data yourself when moving. Back up PBIX files separately.

Field inspection runs locally. AI requests and dictionary generation send the relevant model structure, formulas, messages, attached images, field values or query results to the selected service. Handle backups and exports according to the report’s access requirements.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Blank desktop window or startup failure | Check WebView2 Runtime and `assistant.log` in the data directory. With the source version, start from PowerShell to see errors. |
| Model service returns 401, 403 or 404 | Verify protocol, API base URL, key and model ID, then select **Test connection**. |
| Sending opens Model semantics | Build or enter usable business definitions, then return to the chat and send the draft. |
| Fields visible but queries unavailable | Open the matching Desktop report. Check the connection button and sync if prompted. |
| Values differ from the report | Match the metric, date column, visual filters, currency and units. Check blanks, leading zeros and TopN limits. |
| No write button for generated DAX | Run live validation or the calculated-column sample in Desktop. If the first write cannot download a component, check NuGet connectivity and proxy settings. |
| History missing after changing installation | Check the actual user data directory for each installation and restore the backup as needed. |
| Image request fails | Check format, size and count. Wait for upload to finish and choose a model that supports images. |
