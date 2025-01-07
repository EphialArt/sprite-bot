from interactions import (
    slash_command, File, SlashContext, Client, Intents, SlashCommandChoice, listen, slash_option, OptionType, Attachment, Poll, PollMedia, PollAnswer, Role
)
import aiohttp
import asyncio
from googleapiclient.http import MediaIoBaseDownload
from datetime import datetime
import io
import hashlib
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from google.cloud import firestore
from google.cloud import secretmanager
import os
import json
from PIL import Image
from flask import request

bot = Client(token=os.environ["DISCORD_TOKEN"], intents=Intents.DEFAULT)

# Map for Google Drive folder IDs
FOLDER_MAPPING = {
    "temporary": {
        "item": "1BQEyVG8ylKk7oAp8Jhz9M5gxtMhFO3a4",
        "block": "18P3L5YvbgYGYVLadQTQBD-TeI0tm6YSC",
        "particle": "1sXocLrUNiGZbJGzJld0IZGk-ftnvngwk",
        "misc": "1bwf7tLbc0OFQXwxomH3nelpru6l7PQax",
        "model": "1CofC0kIjF-NwKI6Q2ZBr2fOZ6j9ZJ5pc",
        "painting": "1GIV8xibgHAX1Kv4OSwH3w1Vl5Js_4JM-",
        "entities": "19l75hUh5PQX6cNO54-tkk7MTm0NAhjBy",
        "gui": "1ujlruXhKbsROIdsGJjMEnH4GQ2OuAPQ5"
    },
    "permanent": {
        "item": "1NolwQk9msuyexp584AbCWbkv7K-c3FPh",
        "block": "1QFwm04ug7TKxgdlLlEOv4hV6Hl5By5HX",
        "particle": "1w3pEEtapERgv1hGQAZKQQ8fI5StjFC8K",
        "misc": "1j0BE7E2guJ3WPWXnTOA_vctjsNvt22yL",
        "model": "16lncPCMlHXhQBMVzQvEkXX0-Jyl163fC",
        "painting": "1b2c0QL7caM04smn2L7yKynEtWh4uylQ0",
        "entities": "1VOD3d9PDmzw9czvRh2Lh95qEc2tc0mMx",
        "gui": "13x2lDkeBr78326cxAxjboHsTtCrjCCba"
    }
}

UPLOAD_HISTORY = {}  # Tracks uploaded files
is_found = True
is_found_upscale = True
is_found_recursive = True

def authenticate_db():
    credentials = Credentials.from_service_account_file("credentials_db.json") # Initialize Firestore client with credentials
    db = firestore.Client(credentials=credentials)
    return db

def authenticate_drive():
    SCOPES = ['https://www.googleapis.com/auth/drive']
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    service = build("drive", "v3", credentials=creds)
    return service

def add_sprite(db, sprite_name, creator_id, creator_name, folder,):
    doc_ref = db.collection('sprites').document(sprite_name)
    doc_ref.set({
        'creator_id': creator_id,
        'creator_name': creator_name,
        'sprite_name': sprite_name,
        'folder': folder
    })

def get_sprites(db, sprite_name=None, creator_name=None, creator_id=None, folder=None):
    sprites_ref = db.collection('sprites')
    query = sprites_ref

    if sprite_name:
        query = query.where('sprite_name', '==', sprite_name)
    
    if creator_name:
        query = query.where('creator_name', '==', creator_name)
    
    if folder:
        query = query.where('folder', '==', folder)
    
    if creator_id:
        query = query.where('creator_id', '==', creator_id)

    docs = query.stream()

    results = []
    for doc in docs:
        print(f'Document ID: {doc.id}')
        results.append(doc.to_dict())
    
    if not results:
        print('No documents found!')
    
    return results

def recursive_search(service, parent_folder_id, file_name=None):
    global is_found_recursive
    from googleapiclient.errors import HttpError

    def search_files(query, page_token=None):
        response = service.files().list(
            q=query,
            spaces="drive",
            fields="nextPageToken, files(id, name, parents, modifiedTime)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            pageToken=page_token
        ).execute()
        return response.get("files", []), response.get("nextPageToken", None)

    try:
        if file_name:
            query = f"'{parent_folder_id}' in parents and name = '{file_name}' and trashed = false"
        else:
            query = f"'{parent_folder_id}' in parents and trashed = false"

        page_token = None
        while True:
            files, page_token = search_files(query, page_token)
            print(f"Searching in folder ID: {parent_folder_id}, found {len(files)} files")

            for file in files:
                print(f"Found file: {file['name']} (ID: {file['id']})")
                is_found_recursive = True
                yield file

            # Exit the loop if there are no more pages
            if not page_token:
                break

        # Now search for subfolders
        subfolders_query = f"'{parent_folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        subfolders, _ = search_files(subfolders_query)
        
        for subfolder in subfolders:
            print(f"Searching in subfolder: {subfolder['name']} (ID: {subfolder['id']})")
            yield from recursive_search(service, subfolder['id'], file_name)

    except HttpError as e:
        print(f"An error occurred: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

        return

def upload_to_drive(service, file_name: str, folder_id: str):
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    import os

    global is_found
    global is_found_recursive
    is_found_recursive = False  # Initialize to False before search
    is_found = False  # Keeps track of whether any file with the same name was found

    # New folder where the old files will be moved
    archive_folder_id = "1W9Zw6bRhL3nS6gj4S23YBcIdizaWdesN"

    try:
        # Check if archive folder exists and has correct permissions
        try:
            service.files().get(fileId=archive_folder_id).execute()
            print(f"Archive folder ID '{archive_folder_id}' is valid and accessible.")
        except HttpError as e:
            print(f"Error accessing archive folder ID '{archive_folder_id}': {e}")
            return None

        # Search files recursively
        found_files = list(recursive_search(service, folder_id, file_name))
        
        print(f"Found files: {found_files}")

        if not found_files:
            print(f"No existing file with the name '{file_name}' found in the folder or subfolders.")
            is_found = False
        else:
            for file in found_files:
                file_id = file['id']
                print(f"Processing file ID: {file_id}")
                # Move file to the archive folder
                try:
                    service.files().update(
                        fileId=file_id,
                        addParents=archive_folder_id,
                        removeParents=file["parents"][0],  # Ensure to remove from the correct parent
                        fields='id, parents'
                    ).execute()
                    print(f"Moved existing file: {file['name']} to archive folder")
                    is_found = True
                except HttpError as e:
                    print(f"Error moving file {file['name']} (ID: {file_id}): {e}")

    except HttpError as e:
        print(f"Error searching or moving existing files: {e}")
        return None

    # Only upload if a file was found and moved
    if is_found:
        print(f"Proceeding to upload '{file_name}'.")
        file_metadata = {"name": file_name, "parents": [folder_id]}
        file_path = os.path.join(os.getcwd(), file_name)  # Assuming file_name exists in the current directory
        media = MediaFileUpload(file_path, resumable=True)

        try:
            uploaded_file = service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id",
                supportsAllDrives=True
            ).execute()
            print(f"File uploaded successfully. File ID: {uploaded_file.get('id')}")
            return uploaded_file.get("id")
        except HttpError as e:
            print(f"Error uploading file to Drive: {e}")
            return None
    else:
        print(f"Upload skipped: No existing file named '{file_name}' found in the folder.")

def hash_file_content(file_path: str) -> str:
    """Generate a hash for the file content."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        hasher.update(f.read())
    return hasher.hexdigest()

def download_file(service, file_id):
    global is_found_upscale
    if is_found_upscale == True:
        request = service.files().get_media(fileId=file_id)
        file_data = io.BytesIO()
        downloader = MediaIoBaseDownload(file_data, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            print(f"Download {int(status.progress() * 100)}%.")
        
        file_data.seek(0)  # Go back to the start of the file data
        return file_data
    else:
        print("File not found")
    
def upscale_image(image_source, upscale_factor=5):
    if isinstance(image_source, str):
        # If the source is a file path
        with Image.open(image_source) as img:
            return process_image(img, upscale_factor)
    elif isinstance(image_source, io.BytesIO):
        # If the source is a BytesIO object
        image_source.seek(0)
        with Image.open(image_source) as img:
            return process_image(img, upscale_factor)
    else:
        raise ValueError("Unsupported image source type")

def process_image(img, upscale_factor):
    # Perform nearest neighbor upscale
    new_size = (img.width * upscale_factor, img.height * upscale_factor)
    upscaled_img = img.resize(new_size, Image.NEAREST)
    
    # Save the upscaled image to BytesIO
    upscaled_image_data = io.BytesIO()
    upscaled_img.save(upscaled_image_data, format="PNG")
    upscaled_image_data.seek(0)
    
    return upscaled_image_data

def split_message_on_word_boundary(message, max_length, credits=False):
    if credits:
        # Split each credit line into separate entries
        lines = message.split('\n')
        chunks = []
        current_chunk = ""

        for line in lines:
            if len(current_chunk) + len(line) + 1 > max_length:
                chunks.append(current_chunk.strip())
                current_chunk = line
            else:
                current_chunk += "\n" + line

        if current_chunk:
            chunks.append(current_chunk.strip())
    else:
        # Ensure that each filename is separated by a newline
        lines = message.split() 
        chunks = [] 
        current_chunk = ""
        
        for line in lines: 
            if len(current_chunk) + len(line) + 1 > max_length:
                chunks.append(current_chunk.strip()) 
                current_chunk = line 
            else:
                if current_chunk:  # Ensure there is no leading newline
                    current_chunk += "\n" + line
                else:
                    current_chunk = line

        if current_chunk:
            chunks.append(current_chunk.strip())

    return chunks


@listen()
async def on_ready():
    await bot.synchronise_interactions()
    print("Ready")
    print(f"This bot is owned by {bot.owner}")

@slash_command(name="upload", description="Upload sprite for review")
@slash_option(
    name="folder",
    description="The sprite's folder",
    required=True,
    opt_type=OptionType.STRING,
    choices=[
        SlashCommandChoice(name="Items", value="item"),
        SlashCommandChoice(name="Blocks", value="block"),
        SlashCommandChoice(name="Particles", value="particle"),
        SlashCommandChoice(name="Misc", value="misc"),
        SlashCommandChoice(name="Models", value="model"),
        SlashCommandChoice(name="Painting", value="painting"),
        SlashCommandChoice(name="Entities", value="entities"),
        SlashCommandChoice(name="GUI", value="gui")
    ]
)
@slash_option(
    name="image",
    description="Sprite to be uploaded",
    required=True,
    opt_type=OptionType.ATTACHMENT
)
async def upload_sprite(ctx: SlashContext, folder: str, image: Attachment):
    # Download the image
    async with aiohttp.ClientSession() as session:
        async with session.get(image.url) as response:
            if response.status == 200:
                # Save image locally
                file_path = f"./{image.filename}"
                with open(file_path, "wb") as f:
                    f.write(await response.read())
                await ctx.send("Thank you for your submission!", delete_after=60)
                poll_channel = bot.get_channel("1318971041610993725")
                print(ctx.author.global_name)

                service = authenticate_drive()
                db = authenticate_db()
                perm_folder_id = FOLDER_MAPPING["permanent"].get(folder)
                found_files = list(recursive_search(service, perm_folder_id, image.filename)) 
                if found_files: 
                    file_id = found_files[0]['id'] 
                    print(f"File ID found: {file_id}") 
                else:
                    file_id = None
                    print("File ID not found")
                # Download the file from Google Drive
                if file_id:
                    file_data = download_file(service, file_id)
                    
                    # Send the downloaded file as an attachment
                    image_ = File(file=upscale_image(file_data), file_name=image.filename)
                    await poll_channel.send("Old", files=[image_])

                    upscaled_image_data = upscale_image(file_path)

                    _image = File(file=upscaled_image_data, file_name=f"upscaled_{image.filename}")
                    await poll_channel.send("New", files=[_image])
                
                # Upload to temporary folder immediately
                temp_folder_id = FOLDER_MAPPING["temporary"].get(folder)
                if temp_folder_id:
                    upload_to_drive(service, image.filename, temp_folder_id)
                    if is_found == True:
                        print(f"File uploaded to temporary `{folder}` folder.")
                    else:
                        await ctx.send("File upload failed, no file found or another issue occurred.")
                else:
                    await ctx.send(f"Temporary folder `{folder}` not found.")
                
                # Polling after upload
                if is_found == True:
                    _question = PollMedia(text="Is this acceptable?")
                    _answer_yes = PollAnswer(poll_media=PollMedia(text="Yes"), answer_id=1)
                    _answer_no = PollAnswer(poll_media=PollMedia(text="No"), answer_id=2)
                    _poll = Poll(
                        question=_question,
                        answers=[_answer_yes, _answer_no],
                        duration=12
                    )
                    # Send poll
                    poll_message = await poll_channel.send(content=f"<@&1317840840324022273> {image.filename}", poll=_poll)

                    # Track the results
                    await asyncio.sleep(86405)
                    results_yes = poll_message.answer_voters(answer_id=1)
                    results_no = poll_message.answer_voters(answer_id=2)
                    _yes = 0
                    _no = 0
                    async for x in results_yes: 
                        _yes += 1
                    async for y in results_no: 
                        _no += 1
                    print(f"Yes:{_yes}, No:{_no}")
                    if _yes > _no:
                        perm_folder_id = FOLDER_MAPPING["permanent"].get(folder)
                        if temp_folder_id:
                            upload_to_drive(service, image.filename, perm_folder_id)
                            if is_found == True:
                                print(f"File uploaded to temporary `{folder}` folder.")
                                add_sprite(db, image.filename, ctx.author.id, ctx.author.global_name, folder)
                                await ctx.author.send(f"Your sprite '{image.filename}' was approved!")
                            else:
                                print("File upload failed, no file found or another issue occurred.")
                        else:
                            await ctx.send(f"Temporary folder `{folder}` not found.")
                        
                    else:
                        await ctx.author.send(f"Your sprite '{image.filename}' was denied :(")
                            
                    
                else:
                    await ctx.send("No file with this name could be found, perhaps check your spelling, or the folder you selected?")

@slash_command(name="fetch", description="Get a sprite from the resource pack")
@slash_option(
    name="folder",
    description="The sprite's folder",
    required=True,
    opt_type=OptionType.STRING,
    choices=[
        SlashCommandChoice(name="Items", value="item"),
        SlashCommandChoice(name="Blocks", value="block"),
        SlashCommandChoice(name="Particles", value="particle"),
        SlashCommandChoice(name="Misc", value="misc"),
        SlashCommandChoice(name="Models", value="model"),
        SlashCommandChoice(name="Painting", value="painting"),
        SlashCommandChoice(name="Entities", value="entities"),
        SlashCommandChoice(name="GUI", value="gui")
    ]
)
@slash_option(
    name="name",
    description="Name of sprite to be fetched e.g diamond_sword.png",
    required=True,
    opt_type=OptionType.STRING
)
async def fetch_sprite(ctx: SlashContext, folder: str, name: str):
    await ctx.send("Processing...")
    service = authenticate_drive()
    perm_folder_id = FOLDER_MAPPING["permanent"].get(folder)
    
    found_files = list(recursive_search(service, perm_folder_id, name)) 
    if found_files: 
        file_id = found_files[0]['id'] 
        print(f"File ID found: {file_id}") 
    else:
        print("File ID not found")
        file_id = None
    # Download the file from Google Drive

    if file_id:
        # Download the file from Google Drive
        file_data = download_file(service, file_id)
                    
        # Send the downloaded file as an attachment
        image_ = File(file=upscale_image(file_data), file_name=name)
        await ctx.author.send("Here's the sprite you requested", files=[image_])
    else:
        await ctx.send(f"Seems that sprite '{name}' doesn't exist, perhaps check your spelling?")
    
    await ctx.delete()

@slash_command(name="to-do", description="Retrieves a list of all sprites that haven't been done")
@slash_option(
    name="folder",
    description="The sprite's folder",
    required=True,
    opt_type=OptionType.STRING,
    choices=[
        SlashCommandChoice(name="Items", value="item"),
        SlashCommandChoice(name="Blocks", value="block"),
        SlashCommandChoice(name="Particles", value="particle"),
        SlashCommandChoice(name="Misc", value="misc"),
        SlashCommandChoice(name="Models", value="model"),
        SlashCommandChoice(name="Painting", value="painting"),
        SlashCommandChoice(name="Entities", value="entities"),
        SlashCommandChoice(name="GUI", value="gui")
    ]
)
async def to_do(ctx: SlashContext, folder: str):
    await ctx.send("Processing...", delete_after=60)
    service = authenticate_drive()
    perm_folder_id = FOLDER_MAPPING["permanent"].get(folder)
    found_files = list(recursive_search(service, perm_folder_id))
    timestamp = "2024-12-20T00:00:00.000Z"
    todo = []
    for file in found_files:
        name = file['name']
        modified_time = file['modifiedTime']
        dt = datetime.strptime(modified_time, "%Y-%m-%dT%H:%M:%S.%fZ")
        tdt = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ")
        if dt < tdt:
            todo.append(name)
    todo.sort()
    todo_string = "\n".join(todo)
    
    # Split the message into chunks with word boundaries
    max_length = 1900
    todo_chunks = split_message_on_word_boundary(todo_string, max_length, credits=False)

    await ctx.author.send(f"Here's a list of all textures that haven't been done in the '{folder}' folder:")
    for chunk in todo_chunks:
        await ctx.author.send(f"\n{chunk}")

@slash_command(name="credits", description="Fetch credits by user, folder, sprite name, creator, or all.")
@slash_option(
    name="folder",
    description="The sprite's folder (optional)",
    required=False,
    opt_type=OptionType.STRING,
    choices=[
        SlashCommandChoice(name="Items", value="item"),
        SlashCommandChoice(name="Blocks", value="block"),
        SlashCommandChoice(name="Particles", value="particle"),
        SlashCommandChoice(name="Misc", value="misc"),
        SlashCommandChoice(name="Models", value="model"),
        SlashCommandChoice(name="Painting", value="painting"),
        SlashCommandChoice(name="Entities", value="entities"),
        SlashCommandChoice(name="GUI", value="gui")
    ]
)
@slash_option(
    name="name",
    description="The name of the user who created this sprite (optional)",
    opt_type=OptionType.STRING,
)
@slash_option(
    name="sprite_name",
    description="The name of the sprite you're searching for (optional)",
    opt_type=OptionType.STRING
)
async def credits(ctx: SlashContext, folder: str=None, name: str=None, sprite_name: str=None):
    await ctx.send("Processing...")
    db = authenticate_db()
    results = get_sprites(db, sprite_name, name, folder)

    credits = []
    for result in results:
        _sprite_name = result.get('sprite_name')
        _creator_id = result.get('creator_id')
        _folder = result.get('folder')
        _creator_name = bot.get_user(_creator_id)
        _creator_name = _creator_name.global_name
        credit = f"{_sprite_name} in {_folder} created by: {_creator_name}"
        credits.append(credit)
    credits.sort()
    credits_string = "\n".join(credits)
    
    max_length = 1900
    credits_chunks = split_message_on_word_boundary(credits_string, max_length, credits=True)
    
    await ctx.edit(message="@original", content="Here's a list of the credits you requested:")
    for chunk in credits_chunks:
        if chunk:
            await ctx.send(f"\n{chunk}")
        else:
            await ctx.edit(message="@original", content="Credit couldn't be found, perhaps check your spelling?")

# Define the main function to handle HTTP requests and start the bot

async def run_bot():
    try:
        print("Starting bot...")  # Debug print
        await bot.astart()
        print("Bot started")  # Debug print
    except Exception as e:
        print(f"Error starting bot: {e}")

if __name__ == "__main__":
    loop = asyncio.get_event_loop()

    # Schedule bot start in event loop
    loop.create_task(run_bot())

    # Keep the event loop running
    loop.run_forever()
