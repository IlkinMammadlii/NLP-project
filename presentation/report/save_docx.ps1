# Word step 2 of 2: refresh the contents and save the final .docx (run in its own process; Word may need closing after).
# powershell -ExecutionPolicy Bypass -File save_docx.ps1 -In Detailed_Report.build.docx -Out <dir>\Detailed_Report.docx -Log <dir>\docx.log
param([string]$In, [string]$Out, [string]$Log)
"start $(Get-Date -Format T)" | Out-File $Log
$word = New-Object -ComObject Word.Application
$word.Visible = $false; $word.DisplayAlerts = 0; $word.AutomationSecurity = 3
$doc = $word.Documents.Open($In, $false, $true, $false)
"opened" | Out-File $Log -Append
foreach ($t in $doc.TablesOfContents) { $t.Update() }
"toc updated" | Out-File $Log -Append
$doc.SaveAs2($Out, 16, $false, "", $false, "", $false, $false, $false, $false, $false, 65001, $false, $false, 0, $false, 15)
"saved" | Out-File $Log -Append
