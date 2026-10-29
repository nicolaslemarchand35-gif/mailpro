# Test local OVH EWS - Windows PowerShell 5.1
# Lecture de 5 messages au maximum, creation d'un dossier et d'un brouillon.
# Aucun envoi, deplacement, suppression, changement lu/non lu ou fichier journal.
# Chaque execution cree des objets de test distincts, identifies par un suffixe.
& {
    $ErrorActionPreference = 'Stop'
    $credential = $null
    $secret = $null
    $stage = 'Initialisation'
    function Escape-Xml([string]$value) {
        return [System.Security.SecurityElement]::Escape($value)
    }
    function Invoke-Ews([string]$operation, [string]$body) {
        if ($operation -notin @('FindFolder','FindItem','GetItem','CreateFolder','CreateItem')) {
            throw 'Operation interdite dans ce diagnostic.'
        }
        $envelope = '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"><s:Body>' + $body + '</s:Body></s:Envelope>'
        $params = @{
            Uri = 'https://ex.mail.ovh.net/EWS/Exchange.asmx'
            Method = 'Post'
            Credential = $credential
            ContentType = 'text/xml; charset=utf-8'
            Headers = @{ SOAPAction = "http://schemas.microsoft.com/exchange/services/2006/messages/$operation" }
            Body = [System.Text.Encoding]::UTF8.GetBytes($envelope)
            UseBasicParsing = $true
            MaximumRedirection = 0
            TimeoutSec = 45
            ErrorAction = 'Stop'
        }
        $response = Invoke-WebRequest @params
        $doc = New-Object System.Xml.XmlDocument
        $doc.XmlResolver = $null
        $doc.LoadXml($response.Content)
        $codes = $doc.SelectNodes("//*[local-name()='ResponseCode']")
        if ($codes.Count -eq 0) { throw 'Reponse EWS inattendue.' }
        foreach ($code in $codes) {
            if ($code.InnerText -ne 'NoError') { throw ('EWS : ' + $code.InnerText) }
        }
        return ,$doc
    }
    try {
        Write-Host 'TEST OVH - lecture limitee et creation de deux objets de test' -ForegroundColor Cyan
        Write-Host 'Identifiants envoyes uniquement a ex.mail.ovh.net, en HTTPS.'
        $address = Read-Host 'Votre adresse mail professionnelle'
        $secret = Read-Host 'Votre mot de passe' -AsSecureString
        $credential = [System.Management.Automation.PSCredential]::new($address, $secret)

        $stage = 'Lecture des dossiers'
        $doc = Invoke-Ews 'FindFolder' @'
<m:FindFolder Traversal="Deep"><m:FolderShape><t:BaseShape>IdOnly</t:BaseShape></m:FolderShape><m:IndexedPageFolderView MaxEntriesReturned="100" Offset="0" BasePoint="Beginning"/><m:ParentFolderIds><t:DistinguishedFolderId Id="msgfolderroot"/></m:ParentFolderIds></m:FindFolder>
'@
        $folders = $doc.SelectNodes("//*[local-name()='Folders']/*")
        Write-Host ("OK DOSSIERS : {0} dossiers reperes (lecture limitee a 100)." -f $folders.Count) -ForegroundColor Green

        $stage = 'Recherche des cinq derniers messages'
        $doc = Invoke-Ews 'FindItem' @'
<m:FindItem Traversal="Shallow"><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape></m:ItemShape><m:IndexedPageItemView MaxEntriesReturned="5" Offset="0" BasePoint="Beginning"/><m:SortOrder><t:FieldOrder Order="Descending"><t:FieldURI FieldURI="item:DateTimeReceived"/></t:FieldOrder></m:SortOrder><m:ParentFolderIds><t:DistinguishedFolderId Id="inbox"/></m:ParentFolderIds></m:FindItem>
'@
        $ids = $doc.SelectNodes("//*[local-name()='Items']/*/*[local-name()='ItemId']")
        if ($ids.Count -gt 0) {
            $stage = 'Lecture du contenu des messages'
            $itemXml = ($ids | ForEach-Object { '<t:ItemId Id="' + (Escape-Xml $_.GetAttribute('Id')) + '"/>' }) -join ''
            $body = '<m:GetItem><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape><t:BodyType>Text</t:BodyType><t:AdditionalProperties><t:FieldURI FieldURI="item:Subject"/><t:FieldURI FieldURI="item:Body"/></t:AdditionalProperties></m:ItemShape><m:ItemIds>' + $itemXml + '</m:ItemIds></m:GetItem>'
            $doc = Invoke-Ews 'GetItem' $body
            $items = $doc.SelectNodes("//*[local-name()='Items']/*")
            $bodies = $doc.SelectNodes("//*[local-name()='Items']/*/*[local-name()='Body']")
            Write-Host ("OK LECTURE : {0} elements recuperes ; {1} corps de message disponibles. Contenu non affiche." -f $items.Count, $bodies.Count) -ForegroundColor Green
            $doc = $null
        } else {
            Write-Host 'INFO : boite de reception vide ; lecture du contenu non testee.' -ForegroundColor Yellow
        }

        $suffix = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + ([guid]::NewGuid().ToString('N').Substring(0,6))
        $folderName = 'TEST Assistant mail ' + $suffix
        $subject = 'TEST - Assistant mail - ' + $suffix
        $stage = 'Creation du dossier de test'
        $body = '<m:CreateFolder><m:ParentFolderId><t:DistinguishedFolderId Id="msgfolderroot"/></m:ParentFolderId><m:Folders><t:Folder><t:DisplayName>' + (Escape-Xml $folderName) + '</t:DisplayName></t:Folder></m:Folders></m:CreateFolder>'
        $doc = Invoke-Ews 'CreateFolder' $body
        if (-not $doc.SelectSingleNode("//*[local-name()='Folders']/*/*[local-name()='FolderId']")) { throw 'Creation du dossier non confirmee.' }
        Write-Host ('OK DOSSIER CREE : ' + $folderName) -ForegroundColor Green

        $stage = 'Creation du brouillon de test'
        $body = '<m:CreateItem MessageDisposition="SaveOnly"><m:SavedItemFolderId><t:DistinguishedFolderId Id="drafts"/></m:SavedItemFolderId><m:Items><t:Message><t:Subject>' + (Escape-Xml $subject) + '</t:Subject><t:Body BodyType="Text">Brouillon de test cree par le diagnostic EWS. Aucun destinataire, aucun envoi. Vous pouvez supprimer ce brouillon apres verification dans Outlook.</t:Body></t:Message></m:Items></m:CreateItem>'
        $doc = Invoke-Ews 'CreateItem' $body
        if (-not $doc.SelectSingleNode("//*[local-name()='Items']/*/*[local-name()='ItemId']")) { throw 'Creation du brouillon non confirmee.' }
        Write-Host ('OK BROUILLON CREE : ' + $subject) -ForegroundColor Green
        Write-Host 'TERMINE : verifiez le dossier et le brouillon dans Outlook.' -ForegroundColor Cyan
        Write-Host 'Vous pourrez supprimer manuellement ces deux objets de test.'
        Write-Host 'Ce test local ne valide pas encore la connexion depuis un hebergement.'
    }
    catch {
        Write-Host ('ECHEC ETAPE : ' + $stage) -ForegroundColor Red
        if ($_.Exception.Response) {
            Write-Host ('HTTP : ' + [int]$_.Exception.Response.StatusCode)
        } elseif ($_.Exception.Message -match '^(EWS : |Reponse EWS|Creation du|Operation interdite)') {
            Write-Host $_.Exception.Message
        } else {
            Write-Host ('TYPE : ' + $_.Exception.GetType().Name)
            Write-Host ('LIGNE : ' + $_.InvocationInfo.ScriptLineNumber)
        }
        Write-Host 'Les etapes deja marquees OK restent effectuees. Ne relancez pas automatiquement.'
    }
    finally {
        $credential = $null
        if ($null -ne $secret) { $secret.Dispose() }
        $secret = $null
        $doc = $null
        $ids = $null
        $items = $null
        $bodies = $null
    }
}
