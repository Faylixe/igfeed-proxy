# igfeed-proxy

[![Deploy](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy)

A proxy application based on Instagram Basic Display API.
Simply click on deployement button above to deploy the application on Heroku
using free dyno. 

## Usage

The goal of this API is to provide a proxy to
[Instagram Basic Display API](https://developers.facebook.com/docs/instagram-basic-display-api/)
in order to prevent exposing token to client by using
traditional token agent application, and some rate
limiting by caching fetched media in memory.

It aims to be ready to use with minimal effort, just run the
[Heroku deploy button](https://heroku.com/deploy) above, fill
parameters, and deploy. An activation link will be displayed
into the application logs that you will use to authorize your
application with your target Instagram account and this is it !

## Features

- In memory cache of your Instagram data.
- Automatic token refreshing.
- Ping it self at fixed interval to prevent from dyno idling.