# Word step 1 of 2: refresh the contents and export the PDF (run in its own process; Word may need closing after).
# powershell -ExecutionPolicy Bypass -File export_pdf.ps1 -In Detailed_Report.build.docx -Out <dir>\Detailed_Report.pdf -Log <dir>\pdf.log -Toc
param([string]$In, [string]$Out, [string]$Log, [switch]$Toc)
"start $(Get-Date -Format T)" | Out-File $Log
$word = New-Object -ComObject Word.Application
$word.Visible = $false; $word.DisplayAlerts = 0; $word.AutomationSecurity = 3
"created" | Out-File $Log -Append
$doc = $word.Documents.Open($In, $false, $true, $false)
"opened" | Out-File $Log -Append
if ($Toc) { foreach ($t in $doc.TablesOfContents) { $t.Update() }; "toc updated" | Out-File $Log -Append }
$doc.ExportAsFixedFormat($Out, 17)
"exported" | Out-File $Log -Append
