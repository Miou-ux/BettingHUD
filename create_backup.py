import zipfile
import os

def backup_project(output_filename='BettingHUD_Backup_06Mai.zip'):
    print(f"Création de la sauvegarde : {output_filename}")
    
    # Dossiers et fichiers à exclure
    exclude_dirs = {'venv', '.git', '__pycache__', '.pytest_cache', '.cursor'}
    exclude_exts = {'.zip', '.pyc'}
    
    with zipfile.ZipFile(output_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk('.'):
            # Modifier 'dirs' en place pour que os.walk ignore les dossiers exclus
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            
            for file in files:
                if any(file.endswith(ext) for ext in exclude_exts):
                    continue
                    
                file_path = os.path.join(root, file)
                
                # Ignorer le fichier zip lui-même s'il est dans le répertoire
                if file == output_filename:
                    continue
                    
                # Ajouter au zip avec un chemin relatif
                arcname = os.path.relpath(file_path, start='.')
                zipf.write(file_path, arcname)
                
    print("Sauvegarde terminée avec succès !")

if __name__ == '__main__':
    backup_project()
