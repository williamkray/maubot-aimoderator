## AI Powered Moderation

this plugin offers experimental support to allow LLM-backed moderation support. this is an incredibly sensitive topic,
and so the following caveats apply:

3. enabling ai moderation in a room will force a greeting every time someone joins as a reminder that messages
   are being sent to an external service. this will expose the api endpoint you are using in full. if you are using an
   endpoint that includes secure information like a request token in the query string or something like that, it will be
   broadcast to the entire room. you really should use an api key sent in headers instead. you have been warned.
4. the LLM prompt asks the LLM to reply with a specifically structured JSON object. if for some reason the code is
   unable to parse the response from the LLM (probably malformed json or including extra commentary in the response) the
   code is set to retry 2 more times... in theory. i've had this happen so infrequently that i actually haven't had a
   chance to test the retry logic. you may encounter un-moderated content if the language model isn't behaving.
5. the endpoint must be compatible with openai's chat completions api (localai.io provides this compatibility, for
   example).

## New Features

- **Configurable Join Notices**: You can now control whether the bot sends a notice when users join rooms.
  - `enable_join_notice`: Set to `true` to enable or `false` to disable join notices (default: `true`)
  - `custom_notice_text`: Customize the notice text sent when users join rooms. If empty, a default message will be used.

- **Message Type Filtering**: Added support for filtering messages by type to prevent unwanted content.
  - `enable_msgtype_filter`: Enable/disable message type filtering (default: `true`)
  - `allowed_msgtypes`: List of allowed message types (default: `m.text`, `m.image`)
  - `allowed_mimetypes`: List of allowed media types for images (default: common image formats)

## Configuration

Edit `base-config.yaml` to configure:

```yaml
# Enable/disable join notices
enable_join_notice: true

# Custom join notice text (optional)
custom_notice_text: ""

# Other existing configuration options...
admins:
  - '@user1:server.tld'
uncensor_pl: 1
moderate_files: true
ai_mod_threshold: 7
ai_mod_api_key: myapikeygoeshere
ai_mod_api_endpoint: 'http://localhost:8000/v1/chat/completions'
ai_mod_api_model: 'my-favorite-llama-model'

# Message type filtering configuration
enable_msgtype_filter: true
allowed_msgtypes:
  - "m.text"
  - "m.image"
allowed_mimetypes:
  - "image/jpeg"
  - "image/png"
  - "image/webp"
  - "image/gif"  # Non-animated GIF only
```

# installation

install this like any other maubot plugin: zip the contents of this repo into a file and upload via the web interface,
or use the `mbc` utility to package and upload to your maubot server.

be sure to give your bot permission to redact messages from other users, otherwise features will not work!
